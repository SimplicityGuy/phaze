"""The per-enrich-stage orphaned/stuck (recovery-candidate) count and its off-request cache.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr). The raising core, the
degrade-safe wrapper, the module-scope cache and the off-request refresher are one unit by
construction: D-03's "keep the last-good value on failure" contract is only expressible with all
four visible together.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

from phaze.services.pipeline.common import _BUSY_FUNCTION_TO_STAGE
from phaze.services.pipeline.jobs import _LIVE_KEYS_SQL


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


async def get_stage_orphan_counts(session: AsyncSession) -> dict[str, int]:
    """Return the per-enrich-stage orphaned/stuck (recovery-candidate) count, degrade-safe (Phase 87, UI-05/D-05).

    orphan(stage) = the number of ``scheduling_ledger`` rows for the stage's function that are NEITHER
    live (a queued/active ``saq_jobs`` key) NOR domain-completed NOR owned by an in-flight ``cloud_job``
    NOR HELD awaiting cloud (a ``cloud_job(status='awaiting')`` sidecar) -- i.e. EXACTLY the set
    :func:`phaze.tasks.reenqueue.recover_orphaned_work` would re-enqueue for that stage. Parity with
    recovery is DEFINITIONAL (T-87-31 / OQ-2): this reuses recovery's OWN classification predicate
    (``is_domain_completed`` + the per-stage done-set derivation ``_build_done_sets`` + BOTH cloud
    exclusions ``_in_flight_cloud_job_ids`` and ``_awaiting_cloud_job_ids``) rather than re-deriving the
    done clauses here, so the amber rail badge can never drift from what recovery does (phaze-w0yr:
    the ``_awaiting_cloud_job_ids`` fourth exclusion was added to match recovery's 83-06 filter).

    Returns ``{metadata, analyze}`` -> int (the two :data:`STAGE_TO_FUNCTION` enrich functions
    ``extract_file_metadata`` / ``process_file``); ``push_file`` / the controller functions are NOT
    part of the per-enrich badge.

    No staleness threshold is used, so the naive-``enqueued_at`` footgun (Pitfall 4, project memory)
    never bites here -- the only naive/aware comparison is the D-10 metadata cell inside
    ``is_domain_completed``, which already coerces the naive ledger stamp to UTC-aware (CR-02).

    Failure isolation (T-87-28): the whole derivation runs inside a SAVEPOINT
    (``session.begin_nested()``); on ANY DB error the nested scope is rolled back ALONE -- recovering
    the aborted Postgres transaction WITHOUT expiring the dashboard's already-loaded ORM objects (a
    plain ``session.rollback()`` would 500 the page on the next lazy load) -- and the all-zero default
    is returned. It NEVER raises into the hot 5s /pipeline/stats poll. The ``reenqueue`` import is
    FUNCTION-LOCAL: ``reenqueue`` imports :func:`get_live_job_keys` FROM this module, so a top-level
    import would be circular; deferring it also keeps the agent-worker import boundary intact
    (``reenqueue`` is control-only and must never be loaded merely by importing ``services.pipeline``).

    This is the DEGRADE-SAFE public wrapper (HYG-01 / D-05): it is retained UNCHANGED as the parity
    anchor + tested public surface -- delegating to the RAISING :func:`_compute_stage_orphan_counts`
    core and swallowing any error into the all-zero default. The parity guard
    (``test_orphan_count.py::test_orphan_count_matches_recovery_candidate_set``) targets this contract.
    """
    try:
        return await _compute_stage_orphan_counts(session)
    except Exception:
        logger.warning("stage_orphan_counts_degraded", exc_info=True)
        return {"metadata": 0, "analyze": 0}


async def _compute_stage_orphan_counts(session: AsyncSession) -> dict[str, int]:
    """Raising core of :func:`get_stage_orphan_counts` (HYG-01, D-03/D-05).

    Returns the same ``{metadata, analyze}`` dict the wrapper returns on success, but
    RAISES on ANY DB error (it does NOT swallow) -- so the off-request refresher
    (:func:`refresh_stage_orphan_counts`) can distinguish a real success from a degrade and thereby
    keep the last-good cache value on failure instead of poisoning it with all-zeros (D-03).

    The classification predicate is REUSED verbatim from recovery
    (:func:`phaze.tasks.reenqueue.is_domain_completed` + ``_build_done_sets`` + BOTH cloud exclusions
    ``_in_flight_cloud_job_ids`` and ``_awaiting_cloud_job_ids``);
    parity with ``recover_orphaned_work`` is DEFINITIONAL and mutation-tested (D-05). The ``reenqueue``
    / ``scheduling_ledger`` imports stay FUNCTION-LOCAL to break the reenqueue<->pipeline cycle and
    preserve the control-only agent-worker boundary (``tests/shared/core/test_task_split.py``); do NOT hoist.

    phaze-xwaj: the live-broker-keys read below executes :data:`_LIVE_KEYS_SQL` DIRECTLY rather than
    going through the degrade-safe :func:`get_live_job_keys` wrapper. That wrapper SWALLOWS any DB
    error into an empty set via its own nested SAVEPOINT -- which un-aborts the enclosing transaction,
    so the rest of THIS function's raising reads would go on to succeed with ``live == set()``, i.e.
    every genuinely live/in-flight ledger row misclassifies as orphaned. That is exactly the "RAISES
    on ANY DB error" contract this function promises breaking silently: mixing one swallowing read
    into an otherwise-raising core lets a live-keys failure masquerade as a real (inflated) success,
    which :func:`refresh_stage_orphan_counts` would then rebind as the new cache value instead of
    keeping the last-good one (D-03). ``get_live_job_keys`` itself is UNCHANGED and stays the right
    call for its degrade-tolerant consumers (the recovery producer).
    """
    out: dict[str, int] = {"metadata": 0, "analyze": 0}
    async with session.begin_nested():
        # Function-local import (see docstring): break the reenqueue<->pipeline import cycle and
        # preserve the control-only boundary (tests/test_task_split.py).
        from phaze.services.scheduling_ledger import get_ledger_rows  # noqa: PLC0415 -- deferred: keeps the reenqueue<->pipeline cycle broken
        from phaze.tasks.reenqueue import (  # noqa: PLC0415 -- deferred: reenqueue is control-only + imports FROM this module (cycle)
            _CLOUD_OWNED_FUNCTIONS,
            _awaiting_cloud_job_ids,
            _build_done_sets,
            _in_flight_cloud_job_ids,
            _ledger_fids,
            _natural_id,
            is_domain_completed,
        )

        rows = await get_ledger_rows(session)
        # RAISING read (phaze-xwaj) -- deliberately NOT get_live_job_keys, see docstring above.
        live = {row[0] for row in (await session.execute(_LIVE_KEYS_SQL)).all()}
        done_sets = await _build_done_sets(session, _ledger_fids(rows))
        in_flight = await _in_flight_cloud_job_ids(session)
        # phaze-w0yr: mirror recover_orphaned_work's FOUR-way filter. Since 83-06 recovery ALSO
        # excludes any file HELD in AWAITING_CLOUD (a cloud_job(status='awaiting') sidecar; 'awaiting'
        # is deliberately NOT in _in_flight_cloud_job_ids' IN_FLIGHT set) -- the stage_cloud_window
        # drain owns it, so recovery never re-enqueues it. Omitting this set counted files Recover will
        # never re-drive (e.g. legacy pre-83-06 held-file process_file:<id> rows) as phantom stuck-work
        # the amber rail badge could never clear. Read ONCE alongside in_flight (the two are disjoint).
        awaiting = await _awaiting_cloud_job_ids(session)
        for row in rows:
            stage = _BUSY_FUNCTION_TO_STAGE.get(row.function)
            if stage is None:
                continue  # push_file / controller rows are not enrich badges
            # phaze-fc2l: SCOPE both cloud exclusions to the functions the cloud_job owns
            # (_CLOUD_OWNED_FUNCTIONS) -- of the two badge stages only ``process_file`` (analyze) is
            # cloud-owned. Applying them unscoped over the function-agnostic ``_natural_id`` under-counted
            # the metadata badge for a cloud-busy file, whose lost metadata rows recovery DOES re-drive
            # (no cloud second owner). Keeps the badge in parity with recovery.
            cloud_excluded = row.function in _CLOUD_OWNED_FUNCTIONS and (_natural_id(row) in in_flight or _natural_id(row) in awaiting)
            if row.key in live or is_domain_completed(row, done_sets) or cloud_excluded:
                continue
            out[stage] += 1
    return out


# HYG-01 / WR-02 orphan-count cache (Phase 91). The amber /pipeline/stats badge polls every 5s; the
# full derivation above materializes the whole ``scheduling_ledger`` (~44.5K rows in the 2026-06-18
# incident) + the per-stage done-sets, which must NEVER run inline on that hot request path (D-01/D-02).
# A process/module-scope cache (NOT request-scoped -- D-04) is refreshed off-request by the FastAPI
# lifespan's ``_orphan_refresh_loop`` on a short TTL; the request-scoped /pipeline/stats read is O(1).
# NO ``asyncio.Lock`` is needed: a single event loop runs the refresher and the readers, and a whole-
# dict rebind (``_orphan_cache = ...``) between awaits is atomic -- readers see either the old dict or
# the new one, never a torn partial (per RESEARCH "Don't Hand-Roll" -- no manual locking).
_ORPHAN_TTL_SECONDS: float = 4.0  # D-01 discretion: < the 5s poll so the cache is at most one tick stale
_orphan_cache: dict[str, int] = {"metadata": 0, "analyze": 0}  # seeded safe until first success
_orphan_cache_expires_at: float = 0.0


def get_cached_stage_orphan_counts() -> dict[str, int]:
    """Return an O(1) COPY of the module-scope orphan-count cache (D-04). No session, no DB.

    Returns a distinct ``dict`` so a caller mutating the return can never corrupt the module cache.
    This is the hot-path reader the /pipeline/stats poll uses instead of the full derivation.
    """
    return dict(_orphan_cache)


async def refresh_stage_orphan_counts() -> dict[str, int]:
    """Recompute the orphan counts off-request and rebind the module cache on SUCCESS ONLY (D-03).

    Opens its OWN ``async_session`` (independent of any request session), runs the RAISING
    :func:`_compute_stage_orphan_counts`, and rebinds ``_orphan_cache`` (+ its TTL stamp) only when
    the compute succeeds. On ANY exception it propagates the error -- the background
    ``_orphan_refresh_loop`` swallows + logs it -- leaving the prior known-good value intact so a
    transient DB hiccup never poisons the badge to all-zeros (D-03).
    """
    global _orphan_cache, _orphan_cache_expires_at
    from phaze.database import async_session  # noqa: PLC0415 -- deferred: keeps the agent-worker import boundary intact

    async with async_session() as session:
        computed = await _compute_stage_orphan_counts(session)
    # Whole-dict rebind is atomic between awaits (no Lock needed -- see module comment above).
    _orphan_cache = computed
    _orphan_cache_expires_at = time.monotonic() + _ORPHAN_TTL_SECONDS
    return computed
