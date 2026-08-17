"""Operator-triggered orphaned-work recovery."""

from __future__ import annotations

import asyncio
from typing import Any

# Deliberate runtime import, NOT type-only (same rule as the `uuid` suppression elsewhere in this
# package): `Request` appears only in route signatures here, so with `from __future__ import
# annotations` ruff reads it as type-only -- but FastAPI resolves route annotations at RUNTIME via
# get_type_hints, and a TYPE_CHECKING-only `Request` would NameError while building the route.
from fastapi import Request  # noqa: TC002
from fastapi.responses import HTMLResponse

from phaze.database import async_session
from phaze.routers.pipeline._common import _background_tasks, logger, router, templates
from phaze.tasks.reenqueue import recover_orphaned_work


# --- Manual recovery endpoint (Phase 42, D-02/D-05) ---


_recovery_state: dict[str, Any] = {"running": False, "result": None, "failed": False}
"""In-process outcome of the MOST RECENT operator-triggered recovery (phaze-71nz).

``POST /pipeline/recover`` fires recovery as a background task and returns immediately, so the POST
response can only ever say "started". That was the whole shape of the 2026-07-31 defect at the
operator layer: a run that replayed 430 ``s3_upload`` rows into guaranteed-403 jobs returned the
IDENTICAL 200 + "Recovery started — re-enqueuing any orphaned work across all stages" as a run that
recovered cleanly. Nothing the operator could see distinguished them.

This cell is what ``GET /pipeline/recover/status`` polls so the FINAL tally -- specifically its
``unreplayable`` total, the count of orphaned work knowingly NOT re-enqueued -- reaches the operator
who pressed the button. Shape::

    {"running": bool, "result": <recover_orphaned_work return> | None, "failed": bool}

Deliberately process-local and non-durable: it is a UI echo of one click, not a record. The durable
record is the ledger (the un-recovered rows survive) plus the controller logs, both of which now name
the skipped stages explicitly. The API runs as a SINGLE uvicorn process (``Dockerfile`` CMD sets no
``--workers``), so the poll always reaches the process that owns the task; were that to change, this
would need to move to Redis and the poll would degrade to "no recent recovery", never to a wrong
answer.
"""


async def _run_recovery(ctx: dict[str, Any]) -> None:
    """Background coroutine: run the gated all-stages recovery producer (force=True).

    Calls the SAME :func:`recover_orphaned_work` producer the controller startup hook runs
    (Phase 42, D-03), so the manual and automatic recovery paths cannot drift. ``force=True``
    bypasses ONLY the no-op queue-loss DETECT gate (this is the operator-driven cold-boot
    safety net, D-05) -- it never bypasses the per-item deterministic-key dedup, so a forced
    reconcile over a live queue collapses every still-in-flight item to a skipped no-op and
    can NEVER double the backlog (Phase-32 doubling class is closed).

    Per-row failures inside ``recover_orphaned_work`` are already isolated and tallied under
    ``errored`` (phaze-o1xx) rather than raising, so this normally just logs the final tally. The
    ``try/except`` here is the LAST line of defense for a failure the producer itself cannot
    isolate (e.g. the session/DETECT-gate read at the top of the function): the previous
    fire-and-forget ``asyncio.create_task`` had no done-callback and no `except`, so that exception
    was never retrieved -- the operator's HTMX response already said "recovery started" and nothing
    else ever surfaced the failure. Log it here so a failed forced recovery is at least visible in
    the controller logs instead of silently vanishing.

    phaze-71nz: the outcome is ALSO published to :data:`_recovery_state` so the operator who pressed
    the button can see it. The controller log was the only surface before, and an operator driving an
    incident from the UI does not have it -- which is how a run that burned an entire stage into
    ``failed`` could read as an unqualified success. ``finally`` clears ``running`` on every path, so
    a crash cannot wedge the status fragment polling forever.
    """
    try:
        result = await recover_orphaned_work(ctx, force=True)
    except Exception:
        logger.exception("manual recovery trigger failed -- operator saw 'recovery started' with no further result surfaced (phaze-o1xx)")
        _recovery_state.update(running=False, result=None, failed=True)
        return
    _recovery_state.update(running=False, result=result, failed=False)
    logger.info(
        "manual recovery trigger complete",
        detected_loss=result["detected_loss"],
        unreplayable=result.get("unreplayable", 0),
        stages=result["stages"],
    )


@router.post("/pipeline/recover", response_class=HTMLResponse)
async def trigger_recover_ui(request: Request) -> HTMLResponse:
    """HTMX endpoint: manually trigger the gated all-stages recovery pass (Phase 42, D-02/D-05).

    The global DAG "Recover" button posts here. It builds a worker-shaped ``ctx`` from the API
    app -- the module-level :data:`phaze.database.async_session` sessionmaker (same DB as the
    ``saq_jobs`` broker), the lifespan-created ``app.state.controller_queue`` (controller stages),
    and ``app.state.task_router`` (per-agent stages) -- and schedules :func:`recover_orphaned_work`
    with ``force=True`` as a fire-and-forget background task (same ``_background_tasks`` discipline
    as every other pipeline trigger, so a large reconcile never blocks the HTTP response). Because
    the producer runs in the background, this returns immediately with a "recovery started" fragment
    rather than the final per-stage counts. The endpoint calls the SAME producer as controller
    startup, so the manual and automatic recovery paths cannot drift (D-03), and the deterministic-key
    dedup keeps a forced reconcile idempotent (T-42-06/T-42-07) -- it can never 500 on a healthy queue.

    phaze-71nz: the returned fragment now POLLS :func:`recover_status_ui` instead of being the last
    word. "Recovery started" is still true at the instant it is rendered, but it must not remain the
    only thing the operator ever sees -- a run that knowingly leaves a stage un-recovered has to say
    so. ``_recovery_state`` is armed to ``running`` HERE (not inside the background task) so the very
    first poll cannot race in ahead of the task starting and report the PREVIOUS run's tally.
    """
    ctx: dict[str, Any] = {
        "async_session": async_session,
        "queue": request.app.state.controller_queue,
        "task_router": request.app.state.task_router,
    }
    _recovery_state.update(running=True, result=None, failed=False)
    task = asyncio.create_task(_run_recovery(ctx))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recover_response.html",
        context={"request": request},
    )


@router.get("/pipeline/recover/status", response_class=HTMLResponse)
async def recover_status_ui(request: Request) -> HTMLResponse:
    """HTMX poll target: the OUTCOME of the most recent operator-triggered recovery (phaze-71nz).

    The "Recover" POST fires recovery in the background and can only report that it started, so this
    is where the run's actual tally reaches the operator. It renders one of four states from
    :data:`_recovery_state`:

    - **running** -- keeps polling;
    - **failed** -- recovery raised; the operator is told to check the logs and retry;
    - **complete with ``unreplayable > 0``** -- the state this bead exists for. Some orphaned work was
      DELIBERATELY not re-enqueued (its stored payload was time-limited and could not be regenerated),
      so those files are NOT covered by the run. Rendered as a distinct warning naming the stages,
      never as the plain success copy;
    - **complete, all clear** -- the per-stage re-enqueued / already-running totals.

    Read-only and idempotent: it touches no queue and no database, so the poll is free to run at
    whatever cadence the fragment sets and safe to hit directly.
    """
    result = _recovery_state["result"]
    stages: dict[str, dict[str, int]] = (result or {}).get("stages", {}) or {}
    skipped_stages = sorted(fn for fn, tally in stages.items() if tally.get("unreplayable"))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/recover_status.html",
        context={
            "request": request,
            "running": _recovery_state["running"],
            "failed": _recovery_state["failed"],
            "result": result,
            "unreplayable": (result or {}).get("unreplayable", 0),
            "unreplayable_stages": skipped_stages,
            "reenqueued": sum(tally.get("reenqueued", 0) for tally in stages.values()),
            "already_running": sum(tally.get("skipped", 0) for tally in stages.values()),
            "errored": sum(tally.get("errored", 0) for tally in stages.values()),
        },
    )
