"""Control-side tiered multi-backend drain: route AWAITING_CLOUD files across N backends (Phase 69).

THE SINGLE "STAY ONE AHEAD" DRIVER (CLOUDPIPE-01/-05, SCHED-01/-02). Phase 49's per-file router HOLDS
every cloud-routed long file in ``FileState.AWAITING_CLOUD`` (it enqueues NOTHING directly -- that path
was removed in Phase 50 so the window cannot be bypassed). This cron is the ONLY thing that introduces
new dispatch work: every ~5 min it tops each backend's in-flight window up to its per-backend ``cap`` by
dispatching the oldest held files, rank-first. Registered as a SINGLE narrow
``CronJob(stage_cloud_window, "*/5 * * * *")`` on the controller.

Phase 69 tiered scheduler (SCHED-01/-02, D-05): the Phase-50 single-backend window is generalized.
ONCE per tick, under the single advisory lock, the drain snapshots EVERY resolved backend's
``is_available()`` and ``remaining = cap - in_flight_count()`` (the per-backend ``cloud_job``-derived
count that D-05-retired the global FileState ``{PUSHING, PUSHED}`` window). It then SELECTs up to
``sum(remaining over available backends)`` AWAITING_CLOUD files ``ORDER BY created_at ASC`` (FIFO)
``FOR UPDATE SKIP LOCKED`` and routes each candidate to the pure ``select_backend`` policy's rank-first
choice, calling ``backend.dispatch()`` and decrementing that backend's local ``remaining``. The
snapshot + SELECT + all dispatches happen in ONE transaction with ONE post-loop commit, so two
overlapping ticks serialize on the lock and no backend's ``cap`` is ever overshot (SCHED-02); a full
top-rank backend spills the candidate to the next rank within the same tick (SCHED-01).

TWO no-op gates, both a clean hold (NOT a raise -- T-50-cron-raise) when unavailable:
  1. PER-BACKEND availability (the snapshot): a backend whose ``is_available()`` is False (compute:
     no compute agent; kueue: cluster unreachable; local: always up) contributes 0 free slots. When
     ALL backends are down/full the tick is a clean no-op ``{"staged": 0, "skipped": 0}``.
  2. FILESERVER agent (the push initiator -- it owns the media mount and runs rsync/upload): absent
     during a rolling restart -> ``{"staged": 0, "skipped": len(candidates)}``, files stay
     AWAITING_CLOUD and re-stage on a later tick.

A double-tick collapses via the deterministic ``push_file:<id>`` key (SAQ dedups the repeat enqueue
to ``None`` -> counted as skipped); the file still flips to PUSHING (the already-live job lands it),
so the per-backend in-flight count stays honest. ``select_backend`` returning ``None`` is a clean hold
(the file stays AWAITING_CLOUD this tick, no writer touches the parked row -- its ``updated_at`` is the
staleness clock the spill-to-local gate reads).

CONTROL-ONLY: needs both PostgreSQL (``ctx["async_session"]``) and the per-agent enqueuer
(``ctx["task_router"]``), exactly like ``recover_orphaned_work``. Register ONLY in
``phaze.tasks.controller`` -- never the agent worker (``tests/shared/core/test_task_split.py`` enforces the
agent role stays Postgres-free). FastAPI-free: imports neither ``fastapi`` nor ``phaze.routers``,
mirroring the ``recover_orphaned_work`` import discipline.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from sqlalchemy import select, text
import structlog

from phaze.config import get_settings
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob
from phaze.models.file import FileRecord
from phaze.services import cloud_staging
from phaze.services.cloud_budget import CloudBudgetState
from phaze.services.enqueue_router import NoActiveAgentError, select_active_agent
from phaze.services.pipeline import get_cloud_staging_candidates
from phaze.services.route_control import get_route_control


if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.services.backend_selection import BackendSlot


logger = structlog.get_logger(__name__)

# WR-04: a fixed transaction-scoped advisory-lock key that serializes overlapping staging ticks so
# the load-bearing ≤N window cannot be overshot. SAQ does not guarantee non-overlapping cron runs,
# and the window COUNT reads COMMITTED truth -- so two ticks could each read window=0, SKIP LOCKED
# past each other's uncommitted PUSHING flips, and stage up to 2x cloud_max_in_flight. Holding this
# lock across the count+claim makes the second tick block until the first commits, after which it
# sees the committed window. Arbitrary stable constant (phase 50, plan 04); never collides because
# no other code path takes an advisory lock.
_STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY = 5_000_504

# DRAIN-02 (Phase 98): the per-probe availability timeout for the once-per-tick snapshot. Mirrors the
# read-path bound ``services.backends._PROBE_TIMEOUT_SEC`` (1.5s) VERBATIM in value; declared locally
# (not imported) because ``backends`` imports THIS module (the advisory-lock key + push-job key), so a
# top-level ``from ...backends import _PROBE_TIMEOUT_SEC`` would close the backends<->drain import cycle
# the deferred imports in stage_cloud_window exist to avoid. Kept in sync by this comment + the mirrored
# value. Without this bound a HUNG (not raising) cluster probe -- KueueBackend.is_available -> kr8s
# LocalQueue refresh() on an httpx client built ``timeout=None`` -- blocks the snapshot ``await``
# indefinitely WHILE holding pg_advisory_xact_lock, stalling every later drain tick AND every
# KueueBackend.reconcile per-row (they take the same lock). The try/except in the snapshot loop already
# catches RAISES; this wait_for converts a HANG into a TimeoutError that the SAME except treats as
# "unavailable" (0 slots this tick), so one hung cluster degrades to 0 slots instead of stalling the drain.
_PROBE_TIMEOUT_SEC = 1.5

# --- phaze-9sqa: head-of-line starvation guard ------------------------------------------------
#
# The drain used to fetch EXACTLY ``sum(free slots)`` candidates, FIFO. That makes the fetch window a
# hostage of its own oldest rows: when all of them are unroutable (``select_backend`` -> ``None``), the
# tick stages nothing, mutates nothing, and the NEXT tick fetches the SAME rows. Production 2026-07-27/28:
# limit=3, the three oldest candidates all at the D-04 attempts cap while the local backend was full,
# ``staged: 0, skipped: 3`` every 5 min for >24 h, a cloud backend sitting idle with free capacity, and
# ~7,000 awaiting rows queued behind 14 poisoned heads.
#
# The fix is PAGINATION, not a smarter WHERE: the drain keeps fetching successive keyset pages until it
# has filled its free slots or hit the scan bound, so an unroutable prefix of ANY length is stepped over
# rather than re-read. Routability stays where it belongs -- in the pure ``select_backend`` policy over the
# per-tick snapshot -- instead of being re-derived (and left to drift) in SQL against the DERIV-04-locked
# candidate clause. It also generalizes: the attempts cap is only ONE hold reason, and a WHERE-clause fix
# would have to grow a new conjunct, correctly, for every future one.

# Page size floor. Paging one free slot at a time would issue one round-trip per poisoned head; a floor
# amortizes the walk over a long unroutable prefix (14 heads = ONE page, not 5). Rows fetched are also
# rows LOCKED (``FOR UPDATE ... OF cloud_job``) until the tick commits, which is why this is deliberately
# modest: the lock footprint is real, even though awaiting rows are near-exclusively the drain's own
# (the callback routers touch uploading/submitted rows, not awaiting ones).
_MIN_CANDIDATE_PAGE = 20

# Hard bound on rows examined per tick, across all pages. Caps three costs that all scale with the walk:
# rows locked, the per-candidate ``_cloud_budget_for`` lookups, and time spent holding
# pg_advisory_xact_lock. A lane whose unroutable prefix is longer than this still starves -- but it now
# says so, loudly and every tick, via the repeated-all-held WARNING below, instead of silently.
_MAX_CANDIDATE_SCAN = 500

# How many CONSECUTIVE ticks that hold 100% of what they scanned it takes to escalate from the routine
# per-tick INFO line to a WARNING. 3 ticks at the */5 cadence is ~15 min of a fully-stalled lane -- long
# enough to ride out a transient full window (which clears on the next tick), short enough that the
# >24 h silent stall this bead came from would have been visible on the same morning.
_ALL_HELD_WARN_AFTER_TICKS = 3

# Consecutive all-held tick counter. Process-local by design: it is an alerting heuristic, not state the
# system's correctness depends on, so a controller restart resetting it to 0 costs at most
# ``_ALL_HELD_WARN_AFTER_TICKS`` ticks of re-detection and buys no DB write on the hot path. Only the
# candidate-loop path touches it (see ``_note_all_held_tick``).
_consecutive_all_held_ticks = 0


def _note_all_held_tick(all_held: bool) -> int:
    """Advance (or reset) the consecutive all-held streak and return it (phaze-9sqa).

    ``all_held`` means the tick examined at least one candidate and routed NONE of them. Any tick that
    dispatches even one file resets the streak to 0 -- the lane is demonstrably draining, so whatever is
    holding the rest is head-of-line pressure the drain is already walking past, not a stall.

    Ticks that never reach the candidate loop (cloud disabled, force-local, zero free slots, zero
    candidates, no fileserver agent) deliberately call this NOT AT ALL: they neither confirm nor deny
    starvation, and each already logs its own condition. Folding them in either way would make the streak
    mean something other than "ticks that looked at work and could not move any of it".
    """
    global _consecutive_all_held_ticks
    _consecutive_all_held_ticks = _consecutive_all_held_ticks + 1 if all_held else 0
    return _consecutive_all_held_ticks


async def _bounded_is_available(backend: Any, session: AsyncSession) -> bool:
    """Await ``backend.is_available`` under an ``asyncio.wait_for(_PROBE_TIMEOUT_SEC)`` bound (DRAIN-02).

    The drain-side twin of ``services.backends._probe_one``: a HUNG cluster probe times out instead of
    stalling the whole tick. A timeout raises ``TimeoutError`` (an ``Exception``), which the snapshot
    loop's existing ``except Exception`` already treats as "unavailable, 0 slots" -- so this helper only
    needs to impose the bound, not classify the failure. Every non-timeout raise propagates unchanged to
    that same handler. ``_PROBE_TIMEOUT_SEC`` is read at call time so tests can shrink it via monkeypatch.
    """
    return bool(await asyncio.wait_for(backend.is_available(session), _PROBE_TIMEOUT_SEC))


def push_file_job_key(file_id: uuid.UUID) -> str:
    """Return the deterministic SAQ job key ``push_file:<file_id>`` for a staged push.

    Mirrors ``analysis_enqueue.process_file_job_key``: a double cron tick (or a retried tick) of an
    already-staged file dedups to a no-op via SAQ's per-queue incomplete-set (T-50-double-enqueue).
    ``file_id`` is a server-generated UUID -- no untrusted free-text enters the key.
    """
    return f"push_file:{file_id}"


async def _cloud_budget_for(session: AsyncSession, file_id: uuid.UUID) -> tuple[int, CloudBudgetState | None]:
    """Return ``(this chain's cloud_job.attempts, the durable cross-chain ledger or None)`` for one candidate.

    Both halves of the D-04 budget the pure ``select_backend`` policy enforces, fetched in ONE round trip
    (the same single lookup the attempts-only predecessor cost -- two outer joins off the guaranteed
    ``files`` row, since either sidecar may legitimately be absent):

    * ``cloud_job.attempts`` -- 0 when the file has never been dispatched (no sidecar row). Bounds the
      CURRENT chain. Attempts live on the sidecar, not on ``FileRecord`` (Plan 01 signature note).
    * ``cloud_budget`` -- ``None`` until the file has burned at least one whole chain. Bounds the NUMBER
      of chains (phaze-2mwyo), and is the half that survives ``routers/agent_analysis``'s D-14 reaper:
      the reaper deletes the ``awaiting`` sidecar on every analyze terminal, so reading ``attempts``
      alone made a re-analysed file look brand new and handed it a fresh full budget.
    """
    row = (
        await session.execute(
            select(
                CloudJob.attempts,
                CloudBudget.chains_spent,
                CloudBudget.attempts_spent,
                CloudBudget.node_loss_spent,
                CloudBudget.budget_spent_at,
            )
            .select_from(FileRecord)
            .outerjoin(CloudJob, CloudJob.file_id == FileRecord.id)
            .outerjoin(CloudBudget, CloudBudget.file_id == FileRecord.id)
            .where(FileRecord.id == file_id)
        )
    ).first()
    if row is None:
        # The FileRecord vanished between the candidate SELECT and here (concurrent scan deletion). No
        # budget of either kind -> the candidate simply routes as a fresh file and its dispatch fails
        # harmlessly downstream; never a raise from inside the tick.
        return 0, None
    attempts, chains_spent, attempts_spent, node_loss_spent, budget_spent_at = row
    budget = (
        None
        if budget_spent_at is None
        else CloudBudgetState(
            chains_spent=int(chains_spent),
            attempts_spent=int(attempts_spent),
            node_loss_spent=int(node_loss_spent),
            budget_spent_at=budget_spent_at,
        )
    )
    return int(attempts or 0), budget


async def _next_candidate_page(
    session: AsyncSession,
    snapshot: dict[str, BackendSlot],
    page: list[tuple[FileRecord, datetime]],
    page_limit: int,
    scanned: int,
) -> tuple[list[tuple[FileRecord, datetime]], int] | None:
    """Return the next keyset page + the LIMIT it was fetched with, or ``None`` to end the walk (phaze-9sqa).

    Called once per page, AFTER that page has been routed, so it decides on post-dispatch truth: the
    snapshot's ``remaining`` counters have already been decremented by everything this tick staged.

    Four stop conditions, in order of how often they fire:

    * **no free slots left** -- the window is full; this is the healthy exit, and the ONLY one a
      non-starving lane ever takes (the first page filled the slots, so there is no second query).
    * **short page** -- the fetch returned fewer rows than its LIMIT, so the candidate set is exhausted.
      Nothing further exists to page to.
    * **scan budget spent** -- ``_MAX_CANDIDATE_SCAN`` rows examined. Bounds lock footprint, per-candidate
      attempt lookups, and advisory-lock hold time on a lane whose unroutable prefix is pathological.
    * **empty next page** -- the cursor ran off the end between pages.

    The cursor is the last row's ``(created_at, id)``, matching the query's composite ORDER BY exactly;
    ``created_at`` is immutable and ``id`` breaks its ties, so the walk can neither skip nor re-serve a row.
    """
    free_slots = sum(slot["remaining"] for slot in snapshot.values() if slot["available"])
    if free_slots <= 0 or len(page) < page_limit or scanned >= _MAX_CANDIDATE_SCAN:
        return None
    # Ask for at least _MIN_CANDIDATE_PAGE (amortize the walk over a long unroutable prefix), never more
    # than the scan budget still unspent.
    next_limit = min(max(free_slots, _MIN_CANDIDATE_PAGE), _MAX_CANDIDATE_SCAN - scanned)
    last_file = page[-1][0]
    next_page = await get_cloud_staging_candidates(session, next_limit, after=(last_file.created_at, last_file.id))
    if not next_page:
        return None
    return next_page, next_limit


class _DrainWalk(NamedTuple):
    """What one ``stage_cloud_window`` walk produced: its tally, its binned holds, and its reach."""

    tally: dict[str, int]
    hold_reasons: Counter[str]
    scanned: int
    aborted: bool


async def _snapshot_backend_capacity(backends: list[Any], session: AsyncSession) -> dict[str, BackendSlot]:
    """Probe EVERY backend's availability + free capacity ONCE per tick (SCHED-02 / Pitfall 1).

    Lifted out of ``stage_cloud_window`` unchanged (phaze-vu88k.7): the probes are still issued exactly
    M times, still under the caller's advisory lock, and are still NEVER re-probed inside the candidate
    loop -- the whole reason this is a snapshot and not a live read.
    """
    # SCHED-02 / Pitfall 1: snapshot EVERY backend's availability + free capacity ONCE per tick.
    # is_available (the Kueue cluster probe / compute agent gate / local always-up) and
    # in_flight_count (the D-05 per-backend cloud_job count replacing the retired global window) are
    # each probed exactly M times HERE -- NEVER re-probed inside the candidate loop below.
    snapshot: dict[str, BackendSlot] = {}
    for backend in backends:
        # MKUE-03 / D-07 (research Pitfall 8): per-backend failure isolation for the once-per-tick
        # snapshot. is_available / in_flight_count are SUPPOSED to swallow their own failures (Phase
        # 68's "is_available never raises" discipline), but a raise or timeout that escapes ONE flaky
        # cluster's probe must NOT abort the whole drain tick -- it would starve every healthy backend
        # (and local) of work. Treat a raising/timing-out backend as UNAVAILABLE (0 free slots) for
        # this tick and log (backend_id only -- never a KubeConfig / SecretStr / exception payload
        # carrying creds, T-70-03-02), then continue so the surrounding limit-gate simply sees this
        # backend contribute nothing while every other backend proceeds normally.
        try:
            # DRAIN-02: bound the availability probe with asyncio.wait_for so a HUNG cluster probe
            # (not just a raising one) times out to unavailable rather than stalling the whole tick
            # while holding the advisory lock -- mirrors the read-path bound in services/backends.py.
            available = await _bounded_is_available(backend, session)
            remaining = max(0, backend.cap - await backend.in_flight_count(session))
        except Exception:
            logger.warning("stage_cloud_window: backend snapshot probe failed -> treating as unavailable (0 slots)", backend_id=backend.id)
            snapshot[backend.id] = {"backend": backend, "available": False, "remaining": 0, "cap": backend.cap}
            continue
        snapshot[backend.id] = {
            "backend": backend,
            "available": available,
            "remaining": remaining,
            "cap": backend.cap,
        }
    return snapshot


async def _route_candidate_page(
    session: AsyncSession,
    page: list[Any],
    snapshot: dict[str, BackendSlot],
    cfg: ControlSettings,
    task_router: Any,
    tally: dict[str, int],
    hold_reasons: Counter[str],
) -> bool:
    """Route ONE page of FIFO candidates to ``select_backend``'s rank-first choice (SCHED-01).

    Returns True when the fileserver vanished mid-page -- which ends the WHOLE walk, since the
    fileserver is the push initiator for every backend.

    ``dispatch`` owns the FileState -> PUSHING flip AND the cloud_job write in the CALLER's session
    (D-03), before its enqueue, so the caller's single post-walk commit stays the atomic boundary: this
    function NEVER commits (a mid-loop commit would release the advisory lock + row locks and re-open
    the over-stage class, Landmine L1). A truthy ``dispatch`` return is a genuine stage; False is a
    deterministic-key dedup no-op; either way the cloud_job slot was claimed, so the local remaining is
    decremented. ``select_backend_with_reason`` is imported function-locally for the same reason
    ``stage_cloud_window`` does it -- ``backend_selection`` imports ``backends``, which imports THIS
    module, so a module-top import would close that cycle.
    """
    from phaze.services.backend_selection import select_backend_with_reason  # noqa: PLC0415 -- deferred to break the import cycle

    # SCHED-01: route each FIFO candidate to select_backend's rank-first choice across N backends.
    # dispatch owns the FileState -> PUSHING flip AND the cloud_job write in THIS session (D-03),
    # before its enqueue, so the SINGLE post-loop commit stays the atomic boundary -- dispatch NEVER
    # commits (a mid-loop commit would release the advisory lock + row locks and re-open the
    # over-stage class, Landmine L1). A truthy return is a genuine stage; False is a deterministic-key
    # dedup no-op; either way the cloud_job slot was claimed, so the local remaining is decremented.
    for index, (file, lane_entered_at) in enumerate(page):
        cloud_attempts, cloud_budget = await _cloud_budget_for(session, file.id)
        # models/base.py: created_at/updated_at carry no timezone=True, so create_all yields naive
        # datetimes while a TIMESTAMPTZ migration column hands asyncpg tz-aware ones. Match the
        # candidate's awareness (assume-UTC, the scan_reaper / pipeline_scans convention) so the pure
        # select_backend staleness subtraction (now - lane_entered_at) never raises. lane_entered_at
        # is the awaiting cloud_job.updated_at surfaced by get_cloud_staging_candidates (D-07): the
        # staleness clock lives on the sidecar row, NOT file.updated_at, so it survives Phase 90's
        # removal of the dual-written file.state. The parked row is never mutated here.
        now = datetime.now(UTC)
        if lane_entered_at.tzinfo is None:
            now = now.replace(tzinfo=None)
        target, hold_reason = select_backend_with_reason(lane_entered_at, cloud_attempts, snapshot, now, cfg, cloud_budget=cloud_budget)
        if target is None:
            # Clean per-candidate hold: no eligible backend for this file this tick. No state change
            # -- the file stays AWAITING_CLOUD (guards the updated_at staleness signal, RESEARCH A3).
            # phaze-9sqa: bin the labelled reason. This is the ONLY hold that leaves free slots open
            # (every other skip below claimed a slot), so it is the one the walk must step past.
            hold_reasons[hold_reason] += 1
            tally["skipped"] += 1
            continue
        # WR-02: dispatch re-resolves the fileserver agent per file. Under READ COMMITTED a fileserver
        # revoked by a concurrent session AFTER GATE-2 above but BEFORE this iteration raises
        # NoActiveAgentError straight out of dispatch. Catch it -> CLEAN HOLD of the remaining
        # not-yet-dispatched candidates (this one included), which stay AWAITING_CLOUD (cron NEVER
        # raises). Every dispatch gates its fileserver agent BEFORE any state mutation (CR-01), so the
        # raising file is untouched and the already-staged prior candidates are genuine -> break (NOT
        # rollback), letting the post-loop commit persist that good prior work.
        try:
            dispatched = await target.dispatch(file, session, task_router)
        except NoActiveAgentError:
            remaining = len(page) - index
            logger.info("stage_cloud_window hold: fileserver agent vanished mid-tick", held=remaining)
            tally["skipped"] += remaining
            # phaze-9sqa: the fileserver is the push initiator for EVERY backend, so this ends the
            # whole walk -- fetching further pages could only hold them too (and lock them for it).
            return True
        except Exception:
            # MKUE-03 / D-07 (research Pitfall 8): a GENERIC kube/S3 raise from ONE backend's dispatch
            # (a cluster/bucket error, NOT the fileserver-vanish NoActiveAgentError above) is a clean
            # hold of THIS candidate ONLY -- distinct from the fileserver-vanish break, which affects
            # every remaining dispatch. Each dispatch gates its fileserver agent + runs its fallible
            # S3 setup BEFORE the FileState flip (CR-01), so the common pre-upsert raise touches
            # nothing and the file stays AWAITING_CLOUD. We do NOT roll back here (a mid-loop rollback
            # would drop the advisory lock, Landmine L1); if the raise instead POISONED the txn (a PG
            # statement error), the outer safety net rolls the whole tick back so nothing partial is
            # committed. Count it skipped, log (backend_id only, T-70-03-02), do NOT decrement the slot
            # (no work claimed), and continue so a single flaky cluster cannot starve the other
            # backends. The tick NEVER aborts and NEVER raises.
            logger.warning("stage_cloud_window: backend dispatch failed -> holding this candidate", backend_id=target.id)
            tally["skipped"] += 1
            continue
        # The slot is claimed (cloud_job upserted) on both a genuine stage and a dedup no-op, so
        # decrement the local remaining unconditionally -- this is what makes a full top-rank backend
        # spill the NEXT candidate to the next rank within the same tick (SCHED-01), cap-safe (SCHED-02).
        snapshot[target.id]["remaining"] -= 1
        if dispatched:
            tally["staged"] += 1
        else:
            tally["skipped"] += 1
    return False


async def _walk_candidate_pages(
    session: AsyncSession,
    cfg: ControlSettings,
    snapshot: dict[str, BackendSlot],
    candidates: list[Any],
    limit: int,
    task_router: Any,
) -> _DrainWalk:
    """Walk the paged FIFO candidate set to its end, then commit the tick's single transaction.

    phaze-9sqa: the FIFO walk is paged. Each iteration routes one page, then decides whether the free
    slots it failed to fill justify fetching the next one. Every invariant the single-page loop held is
    unchanged: one transaction, one advisory lock, one post-loop commit, no mid-loop rollback --
    pagination only widens WHICH rows a tick may consider, never when it commits.

    ``aborted`` is the CR-02 safety net having fired: the whole tick was rolled back and the caller must
    report a clean hold WITHOUT the completion logging, exactly as the inline version did by returning
    early from inside the transaction.
    """
    from phaze.services.backends import (  # noqa: PLC0415 -- deferred to break the import cycle
        drop_pending_push_file_enqueues,
        flush_pending_push_file_enqueues,
    )

    tally = {"staged": 0, "skipped": 0}
    # phaze-9sqa keyset-walk state. ``page`` / ``page_limit`` are the current page and the LIMIT it was
    # fetched with; ``scanned`` is every row examined this tick (the ``_MAX_CANDIDATE_SCAN`` budget);
    # ``hold_reasons`` bins each held candidate by the ``select_backend`` filter that rejected it, and
    # feeds both the completion log line and the repeated-all-held WARNING.
    page = candidates
    page_limit = limit
    scanned = 0
    hold_reasons: Counter[str] = Counter()
    # CR-02 safety net (T-50-cron-raise): the candidate loop + the single post-loop commit run under
    # ONE outer guard so an UNEXPECTED raise -- e.g. a Postgres serialization/deadlock surfaced from a
    # session.execute mid-loop (which aborts the txn, so every subsequent statement INCLUDING the final
    # commit raises), or a raise from _cloud_budget_for outside the per-candidate try below -- can
    # NEVER propagate out of this cron. On any such error we roll back the WHOLE tick (discarding every
    # partial/uncommitted write so no phantom dispatch is ever committed) and report a clean hold; the
    # held candidates stay AWAITING_CLOUD and re-stage next tick. This is the ONLY rollback: we never
    # roll back mid-loop (that would end the txn and release the pg_advisory_xact_lock, re-opening the
    # over-stage class, Landmine L1). The advisory-lock scope + single post-loop commit are unchanged.
    try:
        # phaze-9sqa: the FIFO walk is now paged. Each iteration routes one page, then decides whether
        # the free slots it failed to fill justify fetching the next one. Every invariant the single-page
        # loop held is unchanged: one transaction, one advisory lock, one post-loop commit, no mid-loop
        # rollback -- pagination only widens WHICH rows a tick may consider, never when it commits.
        while True:
            scanned += len(page)
            fileserver_vanished = await _route_candidate_page(session, page, snapshot, cfg, task_router, tally, hold_reasons)
            next_page = None if fileserver_vanished else await _next_candidate_page(session, snapshot, page, page_limit, scanned)
            if next_page is None:
                break
            page, page_limit = next_page
        await session.commit()
        # phaze-grzo: fire the s3_upload enqueues KueueBackend.dispatch parked, ONLY now that the
        # cloud_job UPLOADING rows are durably committed -- so the worker-visible job (and its
        # report_uploaded callback) can never precede the row it reads. A parked-but-unfired enqueue
        # on a later flush failure leaves a committed UPLOADING row the staging reaper spills back.
        await cloud_staging.flush_pending_s3_enqueues(session)
        # phaze-s5sz: same discipline for the push_file enqueues ComputeAgentBackend.dispatch parked
        # -- fire them ONLY now that the cloud_job SUBMITTED rows are durably committed, so a FAST
        # rsync push's /pushed callback (whose ONLY guard is cloud_job.status == 'submitted') can
        # never precede the committed row it reads.
        await flush_pending_push_file_enqueues(session)
    except Exception:
        # A poisoned transaction (or any unexpected raise from the pre-dispatch loop body / the commit)
        # must degrade to a clean hold, never a cron raise. Roll back the whole tick -- this discards any
        # uncommitted partial write, so a dispatch that raised AFTER a DB mutation can never leave a
        # committed limbo/phantom row -- and report every candidate held; they stay AWAITING_CLOUD.
        # phaze-grzo: drop the parked s3_upload enqueues too -- firing a job whose cloud_job upsert
        # was just rolled back is the ORPHANING half of the enqueue-before-commit hole. phaze-cws5:
        # this is now ALSO the only remaining chance to abort the multipart(s) those dropped
        # enqueues named -- once the rollback below completes their upload_id was never persisted
        # anywhere, so nothing else can ever find them to clean up.
        await cloud_staging.drop_pending_s3_enqueues(session)
        # phaze-s5sz: same for the parked push_file enqueues.
        drop_pending_push_file_enqueues(session)
        logger.warning("stage_cloud_window: tick aborted by an unexpected error -> rolling back, holding all", exc_info=True)
        await session.rollback()
        # phaze-9sqa: ``scanned`` (every row this tick examined across all pages), not the first page's
        # length -- the rollback discards the whole tick, so everything walked is held. It is at least
        # ``len(candidates)``, so the single-page abort still reports exactly what it always did.
        return _DrainWalk({"staged": 0, "skipped": max(scanned, len(candidates))}, hold_reasons, scanned, aborted=True)
    return _DrainWalk(tally, hold_reasons, scanned, aborted=False)


def _log_drain_outcome(walk: _DrainWalk, limit: int, fileserver_agent: Any) -> None:
    """Log what the tick did, and raise the phaze-9sqa starvation alarm when it did nothing.

    Called ONLY on a tick that completed its walk -- an aborted (rolled-back) tick reports its clean
    hold without touching the streak, exactly as before, because it never reached this code.
    """
    tally, hold_reasons, scanned = walk.tally, walk.hold_reasons, walk.scanned
    # phaze-9sqa starvation alarm. An "all-held" tick examined candidates and routed NONE of them -- which,
    # sustained, is the exact production signature this bead came from: free cloud capacity, a non-empty
    # awaiting queue, and ``staged: 0`` forever. One such tick is unremarkable (a full window clears on the
    # next); _ALL_HELD_WARN_AFTER_TICKS in a row is a stalled lane, and the binned reasons say which filter
    # is doing it -- ``cloud_attempts_exhausted`` is the permanent one and means operator action (raise the
    # local cap, or reset/expire those attempts), not patience.
    all_held = tally["staged"] == 0 and sum(hold_reasons.values()) == scanned and scanned > 0
    consecutive_all_held = _note_all_held_tick(all_held)
    if all_held and consecutive_all_held >= _ALL_HELD_WARN_AFTER_TICKS:
        logger.warning(
            "stage_cloud_window: every candidate held on consecutive ticks -- the cloud lane is not draining",
            consecutive_all_held_ticks=consecutive_all_held,
            hold_reasons=dict(hold_reasons),
            candidates_scanned=scanned,
            free_slots=limit,
        )
    logger.info(
        "stage_cloud_window complete",
        agent_id=fileserver_agent.id,
        staged=tally["staged"],
        skipped=tally["skipped"],
        candidates_scanned=scanned,
        hold_reasons=dict(hold_reasons),
    )


async def stage_cloud_window(ctx: dict[str, Any]) -> dict[str, int]:
    """Top each backend's in-flight window up to its ``cap`` by dispatching held files, rank-first.

    See the module docstring for the full tiered-scheduler semantics. Returns
    ``{"staged": N, "skipped": M}`` where ``staged`` counts dispatches that enqueued new work and
    ``skipped`` counts deterministic-key dedup no-ops, clean per-candidate holds (``select_backend``
    -> ``None``), and the held candidate count when the fileserver gate holds. Every early-return path
    (cloud disabled, all backends full/down, no candidates, no fileserver) is a clean no-op that leaves
    the held files in AWAITING_CLOUD for a later tick. NEVER raises (T-50-cron-raise discipline).

    phaze-9sqa: the FIFO fetch is a PAGED keyset walk, not a single ``LIMIT sum(free slots)`` query. The
    first page is still exactly the free-slot count -- a healthy tick issues the identical query and
    returns the identical tally -- but when a page leaves free slots unfilled the drain pages forward
    (:func:`_next_candidate_page`) instead of ending the tick, so an unroutable prefix of any length is
    walked past rather than re-read every 5 min while the queue behind it starves. ``skipped`` therefore
    counts holds across every page walked, and a tick that holds 100% of what it scanned
    ``_ALL_HELD_WARN_AFTER_TICKS`` times running escalates to a WARNING naming the hold reasons.
    """
    # ControlSettings-scoped: this cron is registered ONLY on the control worker (PHAZE_ROLE=control),
    # so get_settings() returns ControlSettings here (mirrors controller.startup's config access).
    cfg = cast("ControlSettings", get_settings())
    # Phase 67 (D-14, REG-04): registry on/off gate. An all-local registry (cloud_enabled False) ->
    # clean no-op BEFORE the advisory lock + snapshot, so the cron introduces NO new dispatch work.
    if not cfg.cloud_enabled:
        return {"staged": 0, "skipped": 0}
    # Deferred imports (Pitfall: keep the module graph acyclic): backends.py re-homes this module's
    # push-job key + compute enqueue leg (it imports this module), and backend_selection imports
    # backends -- importing either at module top would close the backends<->drain import cycle.
    from phaze.services.backends import resolve_backends  # noqa: PLC0415 -- deferred to break the import cycle

    # Phase 69 (SCHED-01): resolve ALL backends (resolve_backends no longer raises on >1 non-local).
    backends = resolve_backends(cfg)

    async with ctx["async_session"]() as session:
        # Phase 71 (BEUI-02, D-08): the force-local override. When engaged, this drain becomes a clean
        # no-op BEFORE the advisory lock + snapshot -- exactly like the cloud_enabled=False early return
        # above -- so it introduces NO new dispatch work and already-held AWAITING_CLOUD files stay held
        # (runbook A4). get_route_control degrades to False (cloud-enabled) on any DB error, so a hiccup
        # never crashes the cron (T-71-03).
        if await get_route_control(session):
            return {"staged": 0, "skipped": 0}
        # WR-04 / SCHED-02: one transaction-scoped advisory lock serializes overlapping ticks so no
        # backend's cap is overshot. The second tick blocks here until the first commits, then its
        # once-per-tick in_flight_count snapshot sees the committed dispatches. Auto-released at txn end.
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _STAGE_CLOUD_WINDOW_ADVISORY_LOCK_KEY})

        snapshot = await _snapshot_backend_capacity(backends, session)
        # Candidate limit = total free slots across all AVAILABLE backends (non-local free capacity +
        # local headroom). GATE 1 for the whole registry: when every backend is full or unavailable the
        # limit is 0 -> clean no-op (no candidate is fetched, no state changes).
        limit = sum(slot["remaining"] for slot in snapshot.values() if slot["available"])
        if limit <= 0:
            return {"staged": 0, "skipped": 0}

        # FIFO oldest-first candidates, bounded to the total free slots, row-locked (one transaction).
        # phaze-9sqa: this is now the FIRST PAGE of a keyset walk rather than the whole window. It stays
        # sized at exactly ``limit``, so a healthy tick -- one whose oldest rows are routable -- issues the
        # same single query over the same rows and produces the same tally as before; the walk below
        # engages ONLY once a page leaves free slots unfilled.
        candidates = await get_cloud_staging_candidates(session, limit)
        if not candidates:
            return {"staged": 0, "skipped": 0}

        # GATE 2: a fileserver agent (the push initiator -- owns the media mount + rsync/upload) must be
        # online for ANY backend to dispatch. Absent during a rolling restart -> clean hold no-op; the
        # locked candidates stay AWAITING_CLOUD (no state change). Never a raise (T-50-cron-raise).
        try:
            fileserver_agent = await select_active_agent(session, kind="fileserver")
        except NoActiveAgentError:
            logger.info("stage_cloud_window hold: no fileserver agent online", candidates=len(candidates))
            return {"staged": 0, "skipped": len(candidates)}

        walk = await _walk_candidate_pages(session, cfg, snapshot, candidates, limit, ctx["task_router"])
    if walk.aborted:
        return walk.tally

    _log_drain_outcome(walk, limit, fileserver_agent)
    return walk.tally
