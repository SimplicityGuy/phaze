---
phase: 71-deployment-config-docs-n-lane-ui
reviewed: 2026-07-05T03:35:33Z
depth: standard
files_reviewed: 16
files_reviewed_list:
  - alembic/versions/031_add_route_control.py
  - src/phaze/main.py
  - src/phaze/models/__init__.py
  - src/phaze/models/route_control.py
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/routing.py
  - src/phaze/routers/shell.py
  - src/phaze/services/backends.py
  - src/phaze/services/route_control.py
  - src/phaze/tasks/release_awaiting_cloud.py
  - src/phaze/templates/pipeline/partials/_analyze_lanes.html
  - src/phaze/templates/pipeline/partials/_lane_card.html
  - src/phaze/templates/pipeline/partials/analyze_workspace.html
  - src/phaze/templates/pipeline/partials/stats_bar.html
  - src/phaze/templates/shell/partials/_force_local_pill.html
  - src/phaze/templates/shell/partials/header.html
findings:
  critical: 0
  warning: 1
  info: 3
  total: 4
warning_resolved: 1
status: resolved
resolution_note: "WR-01 fixed in fe1f0032 (post-probe rollback + hermetic regression test). IN-01/02/03 accepted as advisory (value-identical settings singleton in prod; pre-existing Phase-70 comment; toast accrual matches existing pattern)."
---

# Phase 71: Code Review Report

**Reviewed:** 2026-07-05T03:35:33Z
**Depth:** standard
**Files Reviewed:** 16
**Status:** issues_found

## Summary

Phase 71 adds the BEUI-01 N-lane read-only grid (`get_backend_lane_snapshot` in `services/backends.py`), the BEUI-02 force-local incident toggle (`route_control` table + migration 031 + degrade-safe `get_route_control` reader + `POST /pipeline/routing/force-local` endpoint + header pill), and wires the force-local gate into the drain, both duration-router callers, and the backfill trigger.

Overall this is careful, defensively-written code that closely mirrors existing shipped patterns (`_safe_count` degrade, `pipeline_stage_control` control-row, cloud-card OOB-swap idiom, `pipeline_stages` thin endpoint). I verified the areas of highest risk called out in the task:

- **XSS / template safety:** No user-controlled value reaches a JS/Alpine context. `lane.id`/`lane.kind` render as autoescaped HTML text; `hx-vals` interpolates only a server-side boolean; the toast message and subcount interpolate server-side constants/ints. The prior `|e`-vs-`|tojson` Alpine-context XSS class does **not** recur here. No finding.
- **Endpoint input/auth:** `engage: Annotated[bool, Form()]` is boolean-coerced (no free-text); the write is a single boolean flip on a fixed PK row; state returned is the just-committed value (never optimistic). No app-layer auth is consistent with the internal-realm posture of the sibling pause/resume endpoints. No finding.
- **Async DB / degrade paths:** `get_route_control`, `_admission_by_backend_id`, and `get_backend_lane_snapshot` all follow the guarded-double-rollback → safe-default idiom and are placed as the last read on their request path. The drain force-local gate sits correctly inside the session and before the advisory lock. Migration is additive/reversible with bound-param seed (no SQL injection).
- **Admission/SQL correctness:** The `GROUP BY backend_id` with `count(*) FILTER (...)` aggregates and the `backend_id IS NOT NULL` exclusion match the generalized global predicates. No finding.

One WARNING (degrade-granularity gap that partially defeats a stated per-lane isolation invariant) and three INFO items follow. No blocking defects.

## Warnings

### WR-01: Compute availability-probe DB error collapses the entire lane grid instead of isolating one lane

**File:** `src/phaze/services/backends.py:557-586, 613-638`
**Issue:** The per-lane isolation invariant (T-71-02 — "one hung/failing backend renders that ONE lane offline; every other lane is unaffected") holds only for probe **timeouts**. It does **not** hold for a DB-level error raised by the compute backend's probe.

`_probe_one` awaits `ComputeAgentBackend.is_available(session)`, which runs `select_active_agent(session, kind="compute")` on the **shared request session**. `is_available` only catches `NoActiveAgentError`; a genuine DB error (e.g. `OperationalError`/`InterfaceError`) propagates into `_probe_one`, which swallows it via `except Exception` and returns `(id, False)` **without rolling back the now-aborted transaction**. Control returns to `get_backend_lane_snapshot`, whose loop immediately issues `await backend.in_flight_count(session)` against the poisoned transaction ("current transaction is aborted…"), which raises into the top-level `except`, rolls back, and returns `[]`.

Net effect: a transient DB hiccup *during the compute probe* renders the whole `#analyze-lanes` grid as the muted "Lane status unavailable" panel, rather than showing the compute lane offline and every other (Kueue/local) lane normally. It never 500s (the outer degrade holds), so this is a robustness/UX gap, not a crash — but it silently weakens the isolation guarantee the snapshot advertises. The hung-Kueue timeout case (the primary DoS concern) is unaffected because the Kueue probe uses kr8s, not the session.

**Fix:** Prevent a swallowed probe exception from poisoning the shared session so the loop can still produce per-lane data. Either (a) probe availability on a short-lived session distinct from the one used for `in_flight_count`, or (b) roll back defensively after the probe fan-out before the `in_flight_count` loop, or (c) make each per-lane `in_flight_count` degrade individually. Minimal option (b):

```python
availability = await _probe_availability(session, backends)
# A swallowed DB-level probe error may have aborted the shared txn; recover it so a
# single failing compute probe degrades to that lane offline, not the whole grid.
try:
    await session.rollback()
except Exception:
    logger.warning("lane_snapshot_probe_rollback_failed", exc_info=True)
```

(A rollback here is safe: the snapshot performs no writes, so nothing is lost, and admission/probe results are already materialized in local dicts.)

## Info

### IN-01: Lane snapshot and routing gates read config from two distinct settings instances

**File:** `src/phaze/services/backends.py:614` vs `src/phaze/routers/pipeline.py:396,718,793`
**Issue:** `get_backend_lane_snapshot` resolves the registry via `get_settings()` (the `@lru_cache` singleton, `config.py:925`), while the routing gates in the same request (`trigger_analysis`, `trigger_analysis_ui`, `trigger_backfill_cloud`) read `settings.cloud_enabled` off the module-level `settings` object, which `config.py:983` builds as a **separate** `ControlSettings()` instance. In production both are constructed from the same env/`backends.toml` and never mutated, so they are value-identical — no active bug. But they are genuinely distinct objects (the 71-03 summary documents this split as the reason tests must monkeypatch the function rather than `settings.backends`), so any future runtime config reload, or a test that mutates one, would let the displayed lanes diverge from the lanes the scheduler actually routes across.
**Fix:** Standardize on one source. Prefer `get_settings()` everywhere (or thread the already-resolved settings into the snapshot) so the UI and the routing policy provably read the same registry.

### IN-02: Duplicated comment block in `KueueBackend.reconcile`

**File:** `src/phaze/services/backends.py:431-434`
**Issue:** The two-line `MKUE-01/D-04: thread THIS backend's KubeConfig…` comment is pasted twice back-to-back above the single `_reconcile_one(...)` call. This is pre-existing Phase-70 code (not a Phase-71 change) but sits in a file this review covers. Harmless, but it is dead duplication that should be removed for clarity.
**Fix:** Delete the duplicate two-line comment.

### IN-03: Force-local toast nodes accumulate in `#toast-container`

**File:** `src/phaze/templates/shell/partials/_force_local_pill.html:37-44`
**Issue:** Each toggle appends a toast `<div hx-swap-oob="beforeend:#toast-container">` that is hidden after 5s via `x-show`/`setTimeout` but never removed from the DOM, so repeated toggles grow the container with inert hidden nodes. This matches the existing toast pattern elsewhere in the codebase (cue/tags/tracklist partials), so it is a consistency-preserving choice, and for a single-user admin tool with infrequent incident toggles the growth is negligible. Noted only for completeness.
**Fix (optional):** Add `x-init="setTimeout(() => $el.remove(), 5200)"` (or an `@transition:leave` removal) so dismissed toasts detach from the DOM.

---

_Reviewed: 2026-07-05T03:35:33Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
