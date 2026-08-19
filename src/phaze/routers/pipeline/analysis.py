"""Analysis stage: trigger, duration-routing seam and failure retries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

# The suppression below is deliberate (runtime import, NOT type-only): this module carries
# `from __future__ import annotations`, so ruff offers to move `uuid` into the TYPE_CHECKING block.
# FastAPI resolves route annotations at RUNTIME via get_type_hints, so a `file_id: uuid.UUID` path
# param would raise NameError on import. (Before phaze-0jpe this import also had a plain runtime
# use -- `uuid.uuid4()` for the scan_live_set nonce -- which masked the rule; the annotation
# requirement is the real reason it must stay here.)
import uuid  # noqa: TC003

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import ARRAY, DateTime, String, bindparam, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import IntegrityError

from phaze.config import settings
from phaze.database import get_session
from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.file import FileRecord
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.routers.pipeline._common import _NO_ACTIVE_AGENT_MESSAGE, _background_tasks, _files_retry_oob, logger, router, templates
from phaze.services import enqueue_router
from phaze.services.analysis_enqueue import classify_process_file_collision, enqueue_process_file, process_file_job_key
from phaze.services.backends import hold_awaiting_cloud
from phaze.services.pipeline import get_analysis_failed_files, get_discovered_files_with_duration, get_file_stage_buckets
from phaze.services.route_control import get_route_control
from phaze.services.stage_status import failed_clause


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


def _analysis_file_ids_scope(file_ids: list[uuid.UUID], name: str) -> Any:
    """``AnalysisResult.file_id = ANY(:name)`` -- ONE Postgres array bind, never a bare ``.in_(file_ids)``.

    phaze-r7j9: mirrors ``tasks/reenqueue.py::_fids_scope``, the repo's established idiom for this
    exact problem. A bare ``.in_(file_ids)`` expands to one bind parameter per element, and asyncpg's
    Bind message caps a statement at 32767 parameters -- this module's own docstrings cite ~44.5K-file
    failure sets as the incident scale the bulk retry / cloud-backfill paths were hardened for, well
    past that ceiling. Binding the whole id list as ONE ``uuid[]`` array parameter sidesteps the
    ceiling regardless of list size, and is a single (cheaper) round trip besides.
    """
    return AnalysisResult.file_id == func.any(bindparam(name, value=file_ids, type_=ARRAY(PGUUID(as_uuid=True))))


def _ledger_keys_scope(keys: list[str], name: str) -> Any:
    """``SchedulingLedger.key = ANY(:name)`` -- the same array-bind idiom, string keys (phaze-r7j9)."""
    return SchedulingLedger.key == func.any(bindparam(name, value=keys, type_=ARRAY(String())))


def _scheduling_ledger_cas_delete_stmt(observed_rows: Sequence[Any]) -> Any:
    """CAS-guarded ``DELETE ... WHERE (key, enqueued_at) IN (observed_rows)`` at CONSTANT bind cost.

    phaze-krzz5: a bare ``tuple_(SchedulingLedger.key, SchedulingLedger.enqueued_at).in_(observed_rows)``
    renders a composite ``(key, enqueued_at) IN ((p1, p2), (p3, p4), ...)`` -- SQLAlchemy expands
    each row of a composite IN-list individually, so this is TWO bind parameters per row, hitting
    asyncpg's 32767-parameter cap at roughly HALF the row count the sibling single-column
    ``_analysis_file_ids_scope`` / ``_ledger_keys_scope`` array-bind idiom tolerates (the phaze-r7j9
    fix those two apply, one column short of covering this composite-key DELETE).

    Binds the observed rows as TWO parallel Postgres arrays and matches them via ``unnest`` run in
    lockstep (multiple set-returning functions in one SELECT list iterate together in Postgres),
    extending the same array-bind idiom to a composite key: exactly 2 bind parameters regardless of
    how many rows were observed, same as the single-column helpers above.
    """
    observed_keys = [key for key, _ in observed_rows]
    observed_enqueued_ats = [enqueued_at for _, enqueued_at in observed_rows]
    observed_pairs = select(
        func.unnest(bindparam("cas_delete_keys", value=observed_keys, type_=ARRAY(String()))).label("key"),
        func.unnest(bindparam("cas_delete_enqueued_ats", value=observed_enqueued_ats, type_=ARRAY(DateTime(timezone=True)))).label("enqueued_at"),
    ).subquery()
    return delete(SchedulingLedger).where(
        tuple_(SchedulingLedger.key, SchedulingLedger.enqueued_at).in_(select(observed_pairs.c.key, observed_pairs.c.enqueued_at)),
    )


async def _enqueue_analysis_jobs(queue: Any, files: list[FileRecord], agent_id: str, models_path: str) -> list[uuid.UUID]:
    """Background coroutine to enqueue process_file jobs for a list of files.

    Delegates each enqueue to the FastAPI-free shared producer
    ``services.analysis_enqueue.enqueue_process_file``. That helper owns the
    deterministic job key (``process_file:<file_id>``), the complete 5-field
    ``ProcessFilePayload``, and the job policy (``timeout=0`` + a progress ``heartbeat``,
    ``retries=2``)
    -- so this dashboard path and the Wave-2 agent-reboot re-enqueue path cannot
    drift: both emit the IDENTICAL key, letting SAQ's per-queue deterministic-key
    dedup collapse a repeat enqueue of an in-flight file to a no-op (32-RESEARCH §Q4).

    ``files`` attributes (``id`` / ``original_path`` / ``file_type``) are already
    loaded by ``get_files_by_state`` and the request never commits, so reading them
    here (after the request session may have closed) does not trigger a lazy load.

    All process_file trigger endpoints (``/api/v1/analyze`` + the HTMX
    ``/pipeline/analyze``) funnel through this one helper, so the key + policy are
    applied identically at every enqueue site.

    phaze-4ter: each file's enqueue is now individually contained -- a raised exception
    (e.g. a transient queue-pool error) is logged and the file's id is collected into the
    returned list instead of propagating, so ONE failure can no longer abort every
    remaining enqueue in the group. Returns the ids that failed to enqueue (empty when every
    file succeeded) so a caller that already cleared a durable failure marker BEFORE
    backgrounding this call (:func:`retry_analysis_failed`) can restore it for exactly the
    files that never got a replacement job, instead of the marker and the job both vanishing.

    phaze-ewen: a ``None`` return (deterministic-key collision) is not logged as anything --
    including the case where the key is held by a DEAD job (aborting/failed/stuck), which means
    this file was silently OMITTED from a bulk run the dashboard reports as "N enqueued". This
    does not change behavior (still no retry, and a blocked file is deliberately NOT added to
    ``failed_ids`` -- restoring its ``failed_at`` would be wrong, because the file's marker
    state is whatever the dead key-holder left, and un-wedging the key is the aborting-reaper's
    job, not this loop's), just makes a blocked file visible in logs.

    phaze-p2qvv: the phaze-ewen probe (``queue.job()`` + ``classify_process_file_collision``)
    is itself a second await against the SAQ Postgres broker and can raise (pool timeout,
    connection reset, a version-skewed row failing ``deserialize()``). A merge (537ee6f)
    stitched the phaze-4ter containment and the phaze-ewen probe with the probe sitting OUTSIDE
    the per-file ``try``/``except``, so a raise here escaped this function entirely -- aborting
    every remaining file in the group and, via ``_retry_analysis_group``, skipping the
    ``failed_ids`` restore write too. The probe is purely diagnostic (it only decides whether to
    log; it never changes ``failed_ids`` or control flow), so its own failure is contained here
    and logged instead of being allowed to propagate.
    """
    failed_ids: list[uuid.UUID] = []
    for f in files:
        try:
            job = await enqueue_process_file(queue, f, agent_id, models_path)
        except Exception:
            logger.exception("enqueue_analysis_jobs: failed to enqueue process_file job", file_id=str(f.id))
            failed_ids.append(f.id)
            continue
        if job is not None:
            continue
        try:
            blocked = classify_process_file_collision(await queue.job(process_file_job_key(f.id))) == "blocked"
        except Exception:
            logger.warning(
                "_enqueue_analysis_jobs: collision-classification probe failed -- diagnostic only, enqueue outcome unaffected",
                file_id=str(f.id),
                key=process_file_job_key(f.id),
            )
            continue
        if blocked:
            logger.warning(
                "_enqueue_analysis_jobs: deterministic key held by a dead job -- file omitted from this run",
                file_id=str(f.id),
                key=process_file_job_key(f.id),
            )
    return failed_ids


async def _retry_analysis_group(queue: Any, group: list[FileRecord], agent_id: str, models_path: str) -> None:
    """Background: enqueue one routed group's ``process_file`` jobs for the bulk retry (phaze-4ter).

    :func:`retry_analysis_failed` clears + commits ``analysis.failed_at`` for the WHOLE routed set
    BEFORE backgrounding this call (RESEARCH Pitfall 3 -- the red count must drop regardless of the
    enqueue outcome). That is safe only if a per-file enqueue failure can never be lost: previously
    the background task's done-callback was a bare ``_background_tasks.discard`` that never called
    ``task.result()``, so the FIRST raised enqueue both aborted every remaining file in the group
    (``_enqueue_analysis_jobs`` had no per-file containment) and vanished without a log correlated to
    this request -- the marker was gone, no job was ever enqueued, and nothing recorded that the file
    had ever failed.

    ``_enqueue_analysis_jobs`` now contains each enqueue individually and returns the ids that
    failed; this wrapper re-stamps ``failed_at`` for exactly those ids (a fresh ``async_session`` --
    the request session that cleared the marker is closed by the time this background task runs), so
    a transient enqueue error degrades to "still shows failed, retryable" instead of a silent,
    permanent loss of both the job and the failure record. Any exception this coroutine itself raises
    (e.g. the restore write failing too) is caught and logged here rather than left for a bare
    discard callback to swallow.
    """
    try:
        failed_ids = await _enqueue_analysis_jobs(queue, group, agent_id, models_path)
        if not failed_ids:
            return
        # Deferred import (services/pipeline.py::_read_in_own_session precedent): re-reads
        # `phaze.database.async_session` at CALL time rather than binding this module's
        # import-time reference, so a test that monkeypatches the source attribute onto a
        # per-test connection (`tests/conftest.py::_route_stats_fanout`) is honored here too.
        from phaze.database import async_session  # noqa: PLC0415

        async with async_session() as restore_session:
            # phaze-6ib1n: guard on `analysis_completed_at IS NULL`, mirroring
            # `report_analysis_failed`'s own conflict predicate (routers/agent_analysis.py) and its
            # documented reason -- "guard the failure stamp so it NEVER downgrades a row that already
            # reads DONE". Without it, a file that raced this loop (its own enqueue landed and
            # `put_analysis` stamped `analysis_completed_at` WHILE this background task was still
            # grinding through the rest of the group) trips the `analysis_completed_xor_failed` CHECK.
            # That is a STATEMENT-level violation: this multi-row UPDATE aborts as a whole, the `except`
            # below only logs, and every OTHER id in `failed_ids` -- whose enqueue genuinely never
            # happened -- permanently loses its failure marker with no replacement job. A completed row
            # needs no restore (it is done, not failed), so excluding it here is correct independent of
            # the crash, and it means one raced id can never void the restore for the whole group.
            await restore_session.execute(
                update(AnalysisResult)
                .where(_analysis_file_ids_scope(failed_ids, "restore_ids"), AnalysisResult.analysis_completed_at.is_(None))
                .values(failed_at=func.now(), error_message="retry_analysis_failed: enqueue error, see agent logs (phaze-4ter)"),
            )
            await restore_session.commit()
        logger.error(
            "retry_analysis_failed: restored failed_at marker after enqueue error",
            count=len(failed_ids),
            file_ids=[str(fid) for fid in failed_ids],
        )
    except Exception:
        # phaze-4ter: this coroutine runs detached (asyncio.create_task + a `_background_tasks.discard`
        # done-callback, which never calls `task.result()`) -- an exception escaping here would
        # otherwise surface only via asyncio's uncorrelated "Task exception was never retrieved" GC-time
        # log, never structlog, never tied to this request. Contain and log explicitly instead.
        logger.exception("retry_analysis_failed: background retry group failed")


async def _route_discovered_by_duration(
    app_state: Any,
    session: AsyncSession,
    files_with_duration: list[tuple[FileRecord, float | None]],
    threshold_sec: int,
    cloud_enabled: bool,
    models_path: str,
) -> dict[str, int]:
    """Route each DISCOVERED file to a queue by its duration (Phase 49 seam, reshaped in Phase 50).

    The single per-file routing decision shared by the "Run Analysis" trigger (this module)
    and the Plan-03 backfill producer, so the two paths cannot drift. phaze-c9w9: local routing
    is per-OWNER -- the short/null candidates are grouped by ``FileRecord.agent_id`` via
    :func:`enqueue_router.resolve_queues_for_owned_files` (never one most-recently-seen pick for
    the whole set, which misrouted every other agent's files); each group's queue comes from
    ``app_state.task_router.queue_for`` (the Phase-30 invariant -- never the consumer-less
    default queue).

    Per file, on the captured ``(file, duration)`` tuples:

    - ``duration is None`` or ``< threshold_sec`` AND the file's OWNING fileserver agent is
      online -> enqueue ``process_file`` onto that owner's queue (``local``).
    - ``duration is None`` or ``< threshold_sec`` AND the owning agent is offline -> count as
      ``skipped`` (cannot route locally) -- NO enqueue, NO state change, the run continues.
    - ``duration >= threshold_sec`` -> ALWAYS set the row's state to ``AWAITING_CLOUD``
      (``awaiting``), regardless of whether a compute agent is online (Phase 50 CLOUDPIPE-01).

    Phase 50 reshape (T-50-bypass): there is NO direct-to-compute enqueue here any more. A long
    file is ALWAYS HELD in AWAITING_CLOUD; the bounded ``stage_cloud_window`` controller cron is
    the SINGLE entry to the compute pipeline (it tops the ≤N window up to ``cloud_max_in_flight``
    by staging ``push_file`` for the oldest held files). Holding in exactly one place is what
    makes the window unbypassable -- a 144-file backlog can never blow up the compute scratch
    disk. A held long file is NEVER silently analyzed locally (the load-bearing CLOUDROUTE-02
    safety invariant, T-49-03).

    The held AWAITING_CLOUD UPDATEs are committed with an explicit ``await session.commit()``
    BEFORE the enqueues are backgrounded (``get_session`` does NOT auto-commit -- RESEARCH
    Pitfall 3).

    Returns ``{"local": N, "cloud": 0, "awaiting": K, "skipped": S, "no_active_agent": 0|1}``;
    ``cloud`` is always 0 (no direct compute enqueue remains). ``no_active_agent`` is 1 when
    local candidates exist but NO owning fileserver agent is online (nothing can route locally):
    the caller then surfaces the no-active-agent response, whose template still reports any HELD
    long files (WR-01) via the ``awaiting`` count -- a held long file is real, durable work the
    staging cron will drain.
    """
    local_candidates: list[FileRecord] = []
    skipped = 0
    held = 0
    # phaze-e8kv: tallied separately from `skipped` -- the local-routing block below UNCONDITIONALLY
    # reassigns `skipped` (never accumulates into it), so a count folded into `skipped` inside this
    # loop would be silently discarded before the function returns.
    deleted_before_hold = 0

    for file, duration in files_with_duration:
        # Phase 51 (D-02): when cloud-burst is OFF nothing is "long" -- every file falls to the
        # local branch, so no row is ever held in AWAITING_CLOUD and the cloud pipeline stays dormant.
        is_long = cloud_enabled and duration is not None and duration >= threshold_sec
        if is_long:
            # Phase 50 (CLOUDPIPE-01): ALWAYS hold -- no direct-to-compute path. The bounded
            # stage_cloud_window cron is the single, unbypassable entry to the compute pipeline.
            # Phase 83 (D-01): hold via the shared writer so every go-forward hold carries its
            # cloud_job(status='awaiting', attempts=0) sidecar row -- closing the missing-writer gap
            # that violated the hard shadow invariant AWAITING_CLOUD => cloud_job(status='awaiting')
            # on every held file since migration 032. The helper dual-writes file.state (D-00c) and
            # NEVER commits; the existing post-loop commit below is the hold's own commit boundary.
            #
            # phaze-e8kv: hold_awaiting_cloud's INSERT carries a NOT NULL FK to files.id, and this
            # candidate list was read well before this loop reaches it (a loop over potentially
            # hundreds of files) -- a concurrent delete_scan cascade (services/scan_deletion.py) can
            # remove the FileRecord in that window and FK-violate the INSERT. Mirror force_skip_stage's
            # two-layer discipline (rule 4/5): run the hold inside a SAVEPOINT and treat a caught
            # IntegrityError as "file concurrently deleted -- skip and count it", so one vanished row
            # costs one skipped file instead of an unhandled 500 aborting the whole run (and every
            # earlier hold in the same transaction along with it).
            try:
                async with session.begin_nested():
                    await hold_awaiting_cloud(session, file)
            except IntegrityError:
                logger.info(
                    "route_discovered_by_duration: file deleted before hold could commit; skipping",
                    file_id=str(file.id),
                )
                deleted_before_hold += 1
                continue
            held += 1
        else:
            local_candidates.append(file)

    # Commit the AWAITING_CLOUD held-state UPDATEs BEFORE backgrounding the enqueues
    # (get_session does not auto-commit -- RESEARCH Pitfall 3).
    if held:
        await session.commit()

    # phaze-c9w9: group the local candidates by their OWNING agent and route each group to that
    # agent's queue. A candidate whose owner is offline is SKIPPED (never rerouted to a different
    # agent's mount); NoActiveAgentError here means no candidate's owner is live -- kept as the
    # no_active_agent=1 signal.
    routed_groups: list[tuple[enqueue_router.RoutedQueue, list[FileRecord]]] = []
    no_active_agent = False
    if local_candidates:
        try:
            routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("process_file", app_state, session, local_candidates)
        except enqueue_router.NoActiveAgentError:
            skipped = len(local_candidates)
            no_active_agent = True
        else:
            skipped = len(skipped_files)
    else:
        # No local candidates (an all-long / empty run): preserve the pre-c9w9 signal shape --
        # no_active_agent=1 iff NO fileserver is online at all, so the WR-01 held-count
        # no-active-agent fragment still renders on a cold-boot "Run Analysis" of long files.
        try:
            await enqueue_router.select_active_agent(session, kind="fileserver")
        except enqueue_router.NoActiveAgentError:
            no_active_agent = True

    local = 0
    for routed, group in routed_groups:
        local += len(group)
        local_task = asyncio.create_task(_enqueue_analysis_jobs(routed.queue, group, cast("str", routed.agent_id), models_path))
        _background_tasks.add(local_task)
        local_task.add_done_callback(_background_tasks.discard)

    return {
        "local": local,
        "cloud": 0,
        "awaiting": held,
        "skipped": skipped + deleted_before_hold,
        "no_active_agent": int(no_active_agent),
    }


@router.post("/api/v1/analyze")
async def trigger_analysis(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Enqueue process_file jobs for all DISCOVERED files, routed per-file by duration (Phase 49
    D-06/D-11/D-12; Phase 50 CLOUDPIPE-01 reshape).

    Short/null-duration files route to the FILESERVER queue exactly as before. Long
    (``>= cloud_route_threshold_sec``) files are ALWAYS held in ``AWAITING_CLOUD`` -- there is no
    direct-to-compute enqueue here any more (see :func:`_route_discovered_by_duration`); the
    bounded ``stage_cloud_window`` controller cron is the sole entry to the compute pipeline.
    Short/null files with no fileserver agent online are reported ``skipped`` without aborting the
    run. One SAQ job per locally-routed file; the enqueues run in a background task (via the shared
    router helper) to avoid HTTP timeout on large file counts. Returns the split counts (``cloud``
    is always 0). The no-active-agent message is returned when NO fileserver agent is online
    (nothing can route locally) -- any long files are still committed to ``AWAITING_CLOUD``
    regardless.
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    if not files_with_duration:
        return {"enqueued": 0, "message": "No files in DISCOVERED state"}

    # Phase 71 (BEUI-02, D-08): fold the force-local override into the routing flag. The effective
    # cloud_enabled is ``registry cloud_enabled AND NOT force_local`` -- when forced, nothing is "long"
    # so every file routes local (byte-identical to an all-local registry), and no new row is held in
    # AWAITING_CLOUD. select_backend stays pure (untouched); the flag is read only here at the caller.
    effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        effective_cloud_enabled,
        settings.models_path,
    )

    if counts["no_active_agent"]:
        # Both kinds absent -- nothing was routable. Any long files were still committed to
        # AWAITING_CLOUD (surfaced via the count card); short/null files were skipped.
        return {
            "enqueued": 0,
            "local": 0,
            "cloud": 0,
            "awaiting_cloud": counts["awaiting"],
            "skipped": counts["skipped"],
            "message": _NO_ACTIVE_AGENT_MESSAGE,
        }

    enqueued = counts["local"] + counts["cloud"]
    return {
        "enqueued": enqueued,
        "local": counts["local"],
        "cloud": counts["cloud"],
        "awaiting_cloud": counts["awaiting"],
        "skipped": counts["skipped"],
        "message": (
            f"Enqueued {counts['local']} local, {counts['cloud']} cloud; "
            f"{counts['awaiting']} awaiting cloud, {counts['skipped']} skipped (no local agent)"
        ),
    }


@router.post("/pipeline/analyze", response_class=HTMLResponse)
async def trigger_analysis_ui(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: trigger per-file duration-routed analysis and return the split-count fragment (Phase 49; Phase 50 CLOUDPIPE-01 reshape).

    Mirrors :func:`trigger_analysis`: short/null files route to the fileserver as before; long
    files are ALWAYS held in ``AWAITING_CLOUD`` (no direct-to-compute enqueue -- see
    :func:`_route_discovered_by_duration`), and short/null files with no fileserver online are
    skipped without aborting the run. The fragment reports ``N local, M cloud, K awaiting cloud``
    (+ a skipped bucket); ``cloud`` is always 0. The no-active-agent fragment is rendered when NO
    fileserver agent is online (nothing can route locally).
    """
    files_with_duration = await get_discovered_files_with_duration(session)
    count = len(files_with_duration)

    if count == 0:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/trigger_response.html",
            context={"request": request, "action": "analysis", "count": 0, "no_active_agent": False},
        )

    # Phase 71 (BEUI-02, D-08): same force-local fold as the JSON trigger -- effective cloud_enabled is
    # ``registry cloud_enabled AND NOT force_local``, so a forced registry routes every file local.
    effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)
    counts = await _route_discovered_by_duration(
        request.app.state,
        session,
        files_with_duration,
        settings.cloud_route_threshold_sec,
        effective_cloud_enabled,
        settings.models_path,
    )

    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/trigger_response.html",
        context={
            "request": request,
            "action": "analysis",
            "count": count,
            "no_active_agent": bool(counts["no_active_agent"]),
            "split_counts": True,
            "local": counts["local"],
            "cloud": counts["cloud"],
            "awaiting": counts["awaiting"],
            "skipped": counts["skipped"],
        },
    )


@router.post("/pipeline/analysis-failed/retry", response_class=HTMLResponse)
async def retry_analysis_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated BULK retry of every ANALYSIS_FAILED file (quick-260707-d79).

    ANALYSIS_FAILED is a terminal state that ``recover_orphaned_work`` deliberately treats as
    analyze-DONE (:func:`phaze.tasks.reenqueue._select_done_analyze_ids`), so a genuinely
    un-analyzable file is never auto-looped. This endpoint is that invariant's deliberate,
    operator-gated counterpart: it re-drives EVERY ANALYSIS_FAILED file through the SAME guarded
    funnel every other producer uses -- per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`enqueue_process_file` (COMPLETE ``ProcessFilePayload`` + deterministic
    ``process_file:<id>`` key). There is nothing per-job left to vary: since phaze-w55w1 every
    ``process_file`` analyzes every window of its file, so a retry and a first run are the same
    job (the Phase 44 ``fine_cap`` / ``coarse_cap`` levers and the deepen path they served are
    gone -- ADR-0007 §7).

    Ordering follows the Phase-30 / RESEARCH-Pitfall-3 guards:
    - Resolve the per-agent queue ONCE. ``process_file`` is an AGENT_TASK; if no agent is online
      ``NoActiveAgentError`` is caught and the endpoint returns a fragment WITHOUT flipping any
      state or enqueuing -- it never falls through to the consumer-less default queue.
    - Clear the ``analysis.failed_at`` / ``error_message`` marker, then ``commit`` BEFORE any
      enqueue (get_session does NOT auto-commit): the files leave the red bucket immediately;
      ``put_analysis`` clears it again (a no-op) on success, or ``report_analysis_failed``
      re-stamps it only if it fails AGAIN. Phase 90 (D-09) removed the companion
      ``FileRecord.state = FINGERPRINTED`` reset -- clearing the ``analysis`` marker is now the
      sole required mutation (see the inline note below).
    - The deterministic key dedups any file with a live in-flight job to a no-op, so re-enqueuing
      the WHOLE failed set is safe (dedup-safe; no silent cap).
    - phaze-zecg: the enqueue loop itself runs as a BACKGROUND task (``asyncio.create_task`` + the
      ``_background_tasks`` discipline), not inline in the request -- the state-clearing commit
      above already moved every file off the red bucket, so a client/proxy timeout cancelling this
      HTTP request can no longer leave the tail of a large failed set with its marker cleared but
      no job ever enqueued.
    - phaze-r7j9: the marker-clearing UPDATE binds the routed id list as ONE ``uuid[]`` array
      parameter (:func:`_analysis_file_ids_scope`) instead of a bare ``.in_(...)``, which expands to
      one bind parameter per id and exceeds asyncpg's 32767-parameter cap at the ~44.5K-file incident
      scale this docstring cites -- that used to make the bulk retry itself deterministically 500 at
      the exact scale it exists for.
    - phaze-4ter: :func:`_retry_analysis_group` contains each background enqueue individually and
      restores the marker for any file whose enqueue failed, so a transient queue error can no longer
      silently drop a file off the red bucket with no job and no trace that it ever failed.
    """
    files = await get_analysis_failed_files(session)
    if not files:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: group the failed set by each file's OWNING agent -- never one
        # most-recently-seen pick for the whole set. Files whose owner is offline are skipped
        # (marker left in place, retryable later), never rerouted onto another agent's mount.
        routed_groups, skipped_files = await enqueue_router.resolve_queues_for_owned_files("process_file", request.app.state, session, files)
    except enqueue_router.NoActiveAgentError:
        # Do NOT flip state, do NOT enqueue, do NOT fall through to the default queue (Phase-30).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    if skipped_files:
        logger.warning("retry_analysis_failed: owning agent offline -- files left failed", skipped=len(skipped_files))
    routed_files = [f for _, group in routed_groups for f in group]

    # RESEARCH Pitfall 3: flip out of the terminal bucket and COMMIT before any enqueue so the
    # red count drops on the next 5s poll regardless of the enqueue outcome.
    #
    # Clear the durable `analysis.failed_at` marker and COMMIT before any enqueue so the red count
    # drops on the next 5s poll. Phase 90 (D-09): the paired `files.state = FINGERPRINTED` reset was
    # removed -- `analysis.failed_at` is now the sole failure authority (`failed_clause(Stage.ANALYZE)`,
    # readers cut over in PR-A). Clearing it moves the row off the failed disjunct so it derives
    # `not_started` -- exactly what a fresh re-analysis should see (the XOR CHECK guarantees
    # `analysis_completed_at IS NULL` on a failed row). phaze-c9w9: cleared ONLY for the files that
    # actually route -- clearing a skipped (owner-offline) file's marker would drop it off the red
    # bucket with no job ever enqueued.
    #
    # phaze-r7j9: array-bind the whole routed id list as ONE `uuid[]` parameter
    # (`_analysis_file_ids_scope`) rather than a bare `.in_(...)`, which SQLAlchemy expands to one
    # bind parameter per id -- past asyncpg's 32767-parameter wire cap at the ~44.5K-file incident
    # scale this handler is documented to be hardened for.
    routed_file_ids = [f.id for f in routed_files]
    await session.execute(
        update(AnalysisResult).where(_analysis_file_ids_scope(routed_file_ids, "retry_ids")).values(failed_at=None, error_message=None),
    )
    await session.commit()

    # phaze-zecg: BACKGROUND the enqueue loop -- do not await it inline in the request. The failed
    # set is unbounded (repo incident history documents ~44.5K-job failure sets) and the markers
    # for the WHOLE set were just committed above; an inline loop that a client/proxy timeout
    # cancels mid-way leaves the tail of the set with its failure marker cleared but NO job ever
    # enqueued -- a silent state-loss window between the commit and the last enqueue. Backgrounding
    # via the same `_background_tasks` discipline every other bulk trigger in this router uses
    # (e.g. `_route_discovered_by_duration`, `trigger_metadata_extraction`) removes that window: the
    # response returns immediately and the enqueue loop keeps running to completion regardless of
    # what the client/proxy does with the connection.
    #
    # The single funnel (_enqueue_analysis_jobs -> enqueue_process_file) guarantees the full payload
    # + deterministic dedup key. phaze-4ter: routed through `_retry_analysis_group`, which contains
    # per-file enqueue failures and restores `failed_at` for any file that never got a replacement
    # job, instead of the marker cleared above and the job both silently vanishing.
    for routed, group in routed_groups:
        task = asyncio.create_task(_retry_analysis_group(routed.queue, group, cast("str", routed.agent_id), settings.models_path))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    logger.info("retry_analysis_failed re-queued files", count=len(routed_files))
    return templates.TemplateResponse(
        request=request,
        name="pipeline/partials/retry_failed_response.html",
        context={"request": request, "count": len(routed_files), "no_active_agent": False},
    )


# --------------------------------------------------------------------------------------------------
# Per-file scoped retry variants (87-07 / UI-02 / D-04): the console's per-row Retry on a failed
# enrich cell. Each re-drives ONE file through the SAME Phase-30-hardened guarded funnel the bulk
# endpoints use (``enqueue_router.resolve_queue_for_task`` -> ``NoActiveAgentError`` guard ->
# enqueue), filtered to a single ``file_id`` instead of the whole failed set, and reuses the bulk
# response partials VERBATIM (a count of 1 / 0). The analyze variant preserves the manual-only
# terminal-analyze path (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``): it flips ANALYSIS_FAILED ->
# FINGERPRINTED + clears ``analysis.failed_at`` in ONE transaction and commits BEFORE enqueue (the
# Phase-81 CR-01 rule) so the file leaves the failed disjunct -- it NEVER creates an auto-retry loop
# (the 44.5K over-enqueue guard, behavior 8).
# --------------------------------------------------------------------------------------------------
@router.post("/pipeline/files/{file_id}/analysis-failed/retry", response_class=HTMLResponse)
async def retry_analysis_failed_file(
    request: Request,
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX endpoint: operator-gated PER-FILE retry of ONE ANALYSIS_FAILED file (87-07, UI-02 / D-04).

    The scoped twin of :func:`retry_analysis_failed`: it re-drives EXACTLY ONE file through the
    identical guarded funnel (per-agent routing -> ``NoActiveAgentError`` guard ->
    :func:`enqueue_process_file` with the COMPLETE ``ProcessFilePayload`` + deterministic
    ``process_file:<id>`` key), scoped by ``id == file_id AND`` the derived terminal analyze-failure
    marker (``failed_clause(Stage.ANALYZE)``, Phase 90 PR-A -- no longer the retired
    ``files.state == ANALYSIS_FAILED`` column) so a non-failed (or unknown) file is a safe no-op ack
    (T-87-27 input validation — a UUID path param + the failure-marker guard, never an unscoped
    enqueue).

    MANUAL-ONLY, no auto-loop (D-00b, behavior 8, T-87-24): analyze is the ONLY enrich carve-out
    (``ELIGIBLE_AFTER_FAILURE[ANALYZE]=False``) — a FAILED analyze is terminal and is NEVER
    auto-retried by ``recover_orphaned_work`` / the derived pending set. This endpoint is that
    invariant's deliberate operator-gated counterpart: it clears the ``analysis.failed_at`` /
    ``error_message`` marker, then ``commit`` BEFORE the enqueue (``get_session`` does NOT
    auto-commit) so the file leaves the failed disjunct immediately and derives ``not_started`` for a
    fresh re-analysis. Phase 90 (D-09) removed the companion ``FileRecord.state = FINGERPRINTED``
    reset -- clearing the ``analysis`` marker is now the sole required mutation. The deterministic key
    dedups a live in-flight job to a no-op (T-87-26). The ack is count/bool-only — no operator
    free-text crosses into Jinja (T-d79-04).
    """
    file = (
        # Phase 90 (PR-A, D-09): scope on the DERIVED terminal analyze-failure marker
        # (``failed_clause(Stage.ANALYZE)`` -- an analysis row with ``failed_at`` set), no longer the
        # retired ``files.state == ANALYSIS_FAILED`` column. A non-failed (or unknown) file is a safe no-op.
        await session.execute(select(FileRecord).where(FileRecord.id == file_id, failed_clause(Stage.ANALYZE)))
    ).scalar_one_or_none()
    if file is None:
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False},
        )

    try:
        # phaze-c9w9: route to the FILE's owning agent (agent_id=file.agent_id), never the
        # most-recently-seen fileserver -- an owner-offline file surfaces no_active_agent.
        routed = await enqueue_router.resolve_queue_for_task("process_file", request.app.state, session, agent_id=file.agent_id)
    except enqueue_router.NoActiveAgentError:
        # Do NOT flip state, do NOT enqueue, do NOT fall through to the default queue (Phase-30, T-87-25).
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": True},
        )

    # process_file is an AGENT_TASK -- resolve always returns a non-None agent_id.
    agent_id = cast("str", routed.agent_id)

    # CR-01 (Phase 81): clear the durable `analysis.failed_at` marker and COMMIT before the enqueue so
    # the row leaves the failed disjunct and derives not_started (a fresh re-analysis). Phase 90 (D-09):
    # the paired `files.state = FINGERPRINTED` reset was removed -- `analysis.failed_at` is now the sole
    # failure authority (`failed_clause(Stage.ANALYZE)`, readers cut over in PR-A).
    await session.execute(
        update(AnalysisResult).where(AnalysisResult.file_id == file_id).values(failed_at=None, error_message=None),
    )
    await session.commit()

    # phaze-gcdih: the marker-clear above is ALREADY committed, so an exception raised by the enqueue
    # itself (SAQ's job insert runs on its OWN psycopg3 pool, independent of this session -- see the
    # two-pool analysis at ADR-0003 / pipeline.py:1576-1583) must not be allowed to propagate bare: that
    # would leave the file with no failure marker AND no replacement job, invisible to both the
    # ANALYSIS_FAILED bucket and recover_orphaned_work (ANALYZE is manual-only, D-00b). Mirror the bulk
    # twin's restore (`_retry_analysis_group`): re-stamp the marker on a failed enqueue and tell the
    # operator honestly instead of a dropped htmx 500.
    try:
        job = await enqueue_process_file(routed.queue, file, agent_id, settings.models_path)
    except Exception:
        logger.exception("retry_analysis_failed_file: failed to enqueue process_file job", file_id=str(file_id))
        await session.execute(
            update(AnalysisResult)
            .where(AnalysisResult.file_id == file_id)
            .values(failed_at=func.now(), error_message="retry_analysis_failed_file: enqueue error, see agent logs (phaze-gcdih)"),
        )
        await session.commit()
        return templates.TemplateResponse(
            request=request,
            name="pipeline/partials/retry_failed_response.html",
            context={"request": request, "count": 0, "no_active_agent": False, "enqueue_failed": True},
        )

    if job is None:
        # phaze-k0rv9: a None return is a deterministic-key collision, not a raised exception --
        # but per classify_process_file_collision (services.analysis_enqueue) a key held by a DEAD
        # job (aborting/failed/aborted, or a stuck active row) blocks the enqueue FOREVER: nothing
        # was scheduled and nothing ever will be until the aborting-reaper clears the key. Every
        # other process_file producer classifies this (_enqueue_analysis_jobs
        # phaze-ewen/phaze-p2qvv) -- this endpoint was the one gap, silently
        # reporting success on a marker that was already cleared+committed above. So:
        # classify before claiming success, and degrade a raising probe (the
        # lookup is itself a Postgres-backed SAQ call and can fail transiently) to "in_flight"
        # (benign) rather than letting a diagnostic-only lookup crash this interactive endpoint.
        try:
            collision = classify_process_file_collision(await routed.queue.job(process_file_job_key(file_id)))
        except Exception:
            logger.warning(
                "retry_analysis_failed_file: collision lookup failed -- degrading to already-in-flight",
                file_id=str(file_id),
                key=process_file_job_key(file_id),
                exc_info=True,
            )
            collision = "in_flight"
        if collision == "blocked":
            logger.warning(
                "retry_analysis_failed_file: deterministic key held by a dead job -- retry dropped",
                file_id=str(file_id),
                key=process_file_job_key(file_id),
            )
            # Restore the failure marker: it was cleared+committed above but no replacement job
            # exists, so leaving it clear would silently drop the file from the failed bucket with
            # nothing running and nothing recording that the retry never happened.
            await session.execute(
                update(AnalysisResult)
                .where(AnalysisResult.file_id == file_id)
                .values(failed_at=func.now(), error_message="retry_analysis_failed_file: blocked by a dead job holding the key (phaze-k0rv9)"),
            )
            await session.commit()
            return templates.TemplateResponse(
                request=request,
                name="pipeline/partials/retry_failed_response.html",
                context={"request": request, "count": 0, "no_active_agent": False, "blocked": True},
            )

    logger.info("retry_analysis_failed_file re-queued", file_id=str(file_id))
    # phaze-bgz26: this endpoint's ONLY caller is the Files matrix per-row Retry button
    # (files_table_view.html), and the bucket genuinely changed (failed -> not_started) by the
    # `failed_at` clear + commit above -- but nothing re-renders that row without a full poll
    # this surface never gets (files_table_view.html has NO self-poll by design). Re-derive the
    # bucket AFTER the commit and OOB-push the single Files-matrix pill this write invalidated,
    # the same shape force_skip_stage uses for the record pane (see `_stage_pill_oob`).
    buckets = await get_file_stage_buckets(session, file_id)
    ack = templates.get_template("pipeline/partials/retry_failed_response.html").render(count=1, no_active_agent=False)
    return HTMLResponse(ack + _files_retry_oob(file_id, "analyze", buckets))
