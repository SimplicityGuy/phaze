"""The operator-triggered 1001Tracklists REFRESH sweep -- the only task left in this module.

WHAT USED TO BE HERE, AND WHY IT IS NOT (phaze-2akf)
----------------------------------------------------
``search_tracklist`` and ``scrape_and_store_tracklist`` were the legacy name-search + detail-scrape
pipeline: two SAQ tasks over ``TracklistScraper.scrape_tracklist``, wired to "SEARCH ALL" /
"SCRAPE ALL" buttons an operator could click. They are retired, not merely deprecated, for three
reasons that all point the same way:

1. **The path could not work.** It was httpx-only, so it had no browser and could never clear the
   Turnstile interstitial the detail pages sit behind. Every per-track selector it used matched
   zero nodes against the committed capture, so each run found 52 track containers and extracted
   nothing from each -- a zero-track result indistinguishable from "this set has no tracklist".
2. **It was a second, unaccounted consumer of a whole-host budget.** robots.txt asks ~1 request /
   8 s for the entire 1001Tracklists host. The drain routes every request through
   ``reserve_host_request_slot`` behind a cache, a queue and a priority order; the legacy path
   shared the limiter and none of the rest.
3. **Two per-track selector sets cannot both be maintained.** The one that is fixture-verified
   (``services/tracklist_parser.py``, 52/52 and 12/12 against two real captures) is the one that
   survives.

The replacement is the drain: ``services/tracklist_drain.py`` +
``tasks/tracklist_drain.drain_tracklists``, which collapses derive -> search -> score -> render ->
parse -> persist into ONE operation per unique set.

WHAT REFRESH MEANS NOW (operator decision, 2026-08-03)
------------------------------------------------------
The old ``refresh_tracklists`` was a monthly CRON that re-fetched every tracklist older than 90
days. That is in direct contradiction with the drain's cache, which is built on "a published
tracklist does not change" and therefore never re-fetches. Running both would re-open exactly the
unbounded second consumer described above, on a schedule, forever.

Resolution: **the drain keeps never re-fetching, and refresh becomes on-demand.** It is now a
targeted, operator-triggered sweep for the case the cache genuinely cannot cover -- a page that has
moved or been edited since we read it. There is deliberately:

* **no CronJob** (see ``tasks/controller.py``); and
* **no unbounded default** -- :func:`refresh_tracklists` with no targets does nothing and says so,
  rather than defaulting to "the whole corpus", because a task that sweeps everything when called
  with no arguments is a background policy wearing an on-demand costume.

HOW IT RE-FETCHES WITHOUT RE-SCRAPING
-------------------------------------
This task spends ZERO host requests itself. It re-arms the drain and stops:

1. It DROPS the ``tracklist_lookup_cache`` rows for the targeted pages, so the cache stops
   answering "found, never ask again" for those unique sets.
2. It FLAGS the affected files (:func:`phaze.services.tracklist_priority.flag_files_for_lookup`).
   A flag on an already-tracklisted file is the refresh signal itself: ``build_drain_queue`` lets
   it back past the already-tracklisted filter, and sorts it to the front.

The next ``drain_tracklists`` slice then does the actual work, through the one path, under the one
budget, with the one selector set -- and because
``services.tracklist_drain._resolve_canonical`` APPENDS a version to the existing canonical row
rather than replacing it, the current tracks survive a refresh that comes back blocked or empty.
That is phaze-gfyr's rule ("never overwrite good tracklist data with an empty version") preserved
structurally rather than by an exception: ``perform_lookup`` records a track-less render as
``PARSE_FAILED`` and no version is written at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import CursorResult, delete, select
import structlog

from phaze.enums.tracklist_candidate import LookupOutcome
from phaze.models.tracklist import Tracklist
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.services.tracklist_priority import flag_files_for_lookup


if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)


REFRESHABLE_SOURCE = "1001tracklists"
"""The only ``Tracklist.source`` a refresh can act on.

phaze-p1vy: HISTORICAL rows with ``source="fingerprint"`` and ``source_url=""`` may still exist
from the retired audio-fingerprint path (phaze-0jpe removed it; nothing writes that value any
more). They have no known page, so "re-read the page" is meaningless for them. A positive allowlist
keeps this correct whether or not those rows are ever purged."""


def _parse_uuids(raw: Sequence[str] | None) -> list[uuid.UUID]:
    """Parse operator-supplied ids, dropping (and logging) anything unparseable.

    Mirrors ``tasks/tracklist_drain._parse_file_ids``: one typo in a selection must not fail the
    whole sweep, but it must not vanish silently either.
    """
    parsed: list[uuid.UUID] = []
    for value in raw or ():
        try:
            parsed.append(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError):
            logger.warning("ignoring unparseable refresh id", value=str(value))
    return parsed


async def _resolve_targets(session: AsyncSession, tracklist_ids: list[uuid.UUID], file_ids: list[uuid.UUID]) -> list[Tracklist]:
    """Load the CANONICAL tracklist rows the caller named, by tracklist id and/or by file id.

    Canonical only (``propagated_from_set_key IS NULL``, phaze-fq9h.7): a propagated projection is
    a copy of a page some other file's lookup fetched, and refreshing it directly would either
    re-fetch the same page once per duplicate or leave the canonical row untouched.

    The two clauses are resolved differently (phaze-vtovq), because a NAMED id is not necessarily
    the CANONICAL row's id:

    * ``tracklist_ids`` are matched directly against the canonical filter, unchanged. Naming a
      projection's OWN id resolves to nothing here, deliberately -- that is "right", not a gap.
    * ``file_ids`` need an extra hop. A duplicate file's own ``Tracklist`` row IS a projection --
      ``file_id`` is the duplicate's, ``propagated_from_set_key`` is set -- so ANDing the
      canonical filter directly onto ``file_id.in_(file_ids)`` matched nothing for every
      duplicate, which is exactly backwards from what this docstring used to claim ("naming a
      duplicate's file id still works"). Resolve it in two steps instead: find whichever row(s)
      the named file ids actually own (canonical or projection), read off their ``external_id``
      (the same page, shared by every projection), then load the canonical row for each of those
      pages. :func:`_affected_file_ids` already walks this edge in the other direction
      (``external_id`` -> every file it serves), which is why the docstring read as plausible.

    Results are de-duplicated by row id, so a caller naming both a tracklist id and a file id that
    resolve to the same canonical row gets it back once.
    """
    if not tracklist_ids and not file_ids:
        return []
    by_id: dict[uuid.UUID, Tracklist] = {}
    if tracklist_ids:
        direct = await session.execute(select(Tracklist).where(Tracklist.id.in_(tracklist_ids), Tracklist.is_canonical()))
        for row in direct.scalars():
            by_id[row.id] = row
    if file_ids:
        named = await session.execute(select(Tracklist.external_id).where(Tracklist.file_id.in_(file_ids)))
        external_ids = set(named.scalars().all())
        if external_ids:
            canonical = await session.execute(select(Tracklist).where(Tracklist.external_id.in_(external_ids), Tracklist.is_canonical()))
            for row in canonical.scalars():
                by_id[row.id] = row
    return list(by_id.values())


async def _affected_file_ids(session: AsyncSession, external_ids: Collection[str]) -> set[uuid.UUID]:
    """Every file these pages currently serve -- each canonical row's file plus its projections.

    All of them are flagged, not just the canonical one, because propagation is re-derived from the
    unique set on the next lookup: flagging only the canonical file would refresh the page but
    leave the duplicates pointing at whatever the previous pass wrote if the set's membership has
    since changed.

    phaze-vu88k.4: takes the whole set of pages rather than one page at a time. The caller flags
    the UNION across every refreshed page, so asking per page cost one round trip each to build a
    set that is then unioned anyway. Empty input is a no-op, not an unfiltered scan.
    """
    ids = list(external_ids)
    if not ids:
        return set()
    result = await session.execute(select(Tracklist.file_id).where(Tracklist.external_id.in_(ids), Tracklist.file_id.is_not(None)))
    return {file_id for file_id in result.scalars().all() if file_id is not None}


async def refresh_tracklists(
    ctx: dict[str, Any],
    *,
    tracklist_ids: Sequence[str] | None = None,
    file_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Re-arm the drain for specific, operator-named tracklists. Spends no host requests.

    Args:
        ctx: SAQ context; ``ctx["async_session"]`` is the short-session factory.
        tracklist_ids: canonical ``tracklists.id`` values to refresh.
        file_ids: ``files.id`` values whose tracklist should be refreshed -- the same sweep,
            addressed from the record page's side.

    Returns:
        Counts of what was re-armed. ``status`` is ``"no_targets"`` when the call named nothing:
        that is a REFUSAL, not a corpus-wide default (see the module docstring).

    A refreshed set is not re-fetched here. It re-enters the drain queue, and the next
    ``drain_tracklists`` slice pays for it out of the same whole-host budget as everything else --
    which is the entire point of routing refresh through the drain rather than around it.
    """
    targets = _parse_uuids(tracklist_ids)
    files = _parse_uuids(file_ids)
    if not targets and not files:
        logger.warning("refresh_tracklists called with no targets -- refusing to sweep the corpus")
        return {"status": "no_targets", "requested": 0, "refreshed": 0, "files_flagged": 0, "cache_entries_cleared": 0, "skipped": 0}

    refreshed = 0
    skipped = 0
    flagged: set[uuid.UUID] = set()
    cleared = 0

    async with ctx["async_session"]() as session:
        tracklists = await _resolve_targets(session, targets, files)
        # phaze-vu88k.4: partition FIRST, then do each DB step ONCE for the whole refreshable set.
        # The old shape ran a cache DELETE, an affected-files SELECT and one flag upsert PER FILE
        # inside this loop, so refreshing K pages serving F files cost 2K + F round trips. The skip
        # branch, its log line and every tally below are unchanged -- only the work for the
        # refreshable remainder moved out of the loop.
        refreshable: list[Tracklist] = []
        for tracklist in tracklists:
            if tracklist.source != REFRESHABLE_SOURCE or not tracklist.source_url:
                logger.info(
                    "skipping refresh of a tracklist with no re-readable page",
                    tracklist_id=str(tracklist.id),
                    source=tracklist.source,
                )
                skipped += 1
                continue
            refreshable.append(tracklist)

        if refreshable:
            # Drop the cache's answer for these pages. `record_outcome` keys on set_key, but the
            # external_id is what a caller holding a Tracklist row actually knows, and the positive
            # rows carry it -- so this deletes exactly the entries that would otherwise keep saying
            # "found, never ask again". Anything else (a negative, a transient backoff) is left
            # alone: it is not what is being refreshed.
            #
            # One DELETE over every refreshed external_id reports the same total in `rowcount` as
            # the per-page DELETEs summed to: a page whose entries an earlier iteration had already
            # removed contributed 0 to that sum.
            deleted = await session.execute(
                delete(TracklistLookupCache).where(
                    TracklistLookupCache.external_id.in_([tracklist.external_id for tracklist in refreshable]),
                    TracklistLookupCache.outcome == LookupOutcome.FOUND.value,
                )
            )
            cleared = cast("CursorResult[Any]", deleted).rowcount or 0

            # `flagged` was built by unioning each page's files, so asking for the union directly is
            # the same set -- and one bulk upsert replaces one round trip per file.
            flagged = await _affected_file_ids(session, [tracklist.external_id for tracklist in refreshable])
            await flag_files_for_lookup(session, flagged)
            refreshed = len(refreshable)

        await session.commit()

    logger.info(
        "tracklist refresh re-armed the drain",
        requested=len(targets) + len(files),
        refreshed=refreshed,
        skipped=skipped,
        files_flagged=len(flagged),
        cache_entries_cleared=cleared,
    )
    return {
        "status": "rearmed",
        "requested": len(targets) + len(files),
        "refreshed": refreshed,
        "skipped": skipped,
        "files_flagged": len(flagged),
        "cache_entries_cleared": cleared,
    }
