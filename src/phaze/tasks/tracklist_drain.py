"""SAQ entry point for the 1001Tracklists drain (phaze-fq9h.7).

One job = one BOUNDED SLICE of a months-long pass. That framing is the whole design:

* The host budget is ~1 request / 8 s for the entire system, so a full pass over a ~250,000-file
  archive is measured in months and can never be "one job that finishes". A job that tried would
  be killed by its own timeout with its progress recorded only in whatever it had already
  committed -- which is exactly why the drain commits per candidate.
* Every slice boundary is therefore a free, consistent restart point. ``drain_tracklists`` holds no
  cursor and writes no checkpoint: the next slice rebuilds the queue from the corpus, and the
  lookup cache has already removed everything the previous slices answered. Resumption is a
  property of the data, not of a file that can disagree with it.
* ``limit`` bounds the slice in LOOKUPS (host requests), never in wall clock or files, because
  requests are the scarce resource. The task's own timeout is a backstop, not the mechanism.

There is NO CronJob for this task, deliberately. The epic's ethics bound is that the drain is
operator-initiated rather than a blanket pipeline stage -- it deploys from a residential IP, it
runs a headful browser, and it spends a shared public host's published budget. A cron that started
crawling on its own the moment the container came up would take that decision away from the
operator. The admin UI (phaze-fq9h.8) is the intended trigger.

The renderer's browser is launched ONCE per job and reused across every candidate in the slice.
That is not just an optimisation: the phaze-fq9h.1 capture measured 16/16 first-navigation clears
with one browser reused across the run, against the spike's 6/8 with fresh launches, so a
per-lookup launch would plausibly cost real Turnstile retries -- i.e. real requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import structlog

from phaze.enums.tracklist_candidate import DuplicateConfidence
from phaze.services.tracklist_drain import DEFAULT_LOOKUP_LIMIT, DEFAULT_PROPAGATION_MIN_CONFIDENCE, build_drain_queue, drain_once
from phaze.services.tracklist_priority import load_flagged_file_ids
from phaze.services.tracklist_render import TracklistRenderer
from phaze.services.tracklist_scraper import TracklistScraper


if TYPE_CHECKING:
    from collections.abc import Sequence


logger = structlog.get_logger(__name__)


def _parse_file_ids(raw: Sequence[str] | None) -> list[uuid.UUID]:
    """Parse operator-supplied file ids, dropping anything unparseable rather than failing the job.

    A malformed id in a flag list is a UI/typo problem, and refusing the whole slice over it would
    stop a months-long drain for a cosmetic reason. The bad value is logged so it is not silent.
    """
    parsed: list[uuid.UUID] = []
    for value in raw or ():
        try:
            parsed.append(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError):
            logger.warning("ignoring unparseable flagged file id", value=str(value))
    return parsed


def _parse_confidence(raw: str | None) -> DuplicateConfidence:
    """Resolve the propagation gate, falling back to the SAFE default on anything unrecognized.

    Fails toward ``EXACT``. A typo'd tier must never silently WIDEN the gate: propagating across a
    heuristic duplicate link writes a wrong tracklist onto a genuinely different set and then marks
    it tracklisted, so it is never revisited.
    """
    if raw is None:
        return DEFAULT_PROPAGATION_MIN_CONFIDENCE
    try:
        return DuplicateConfidence(raw)
    except ValueError:
        logger.warning("unrecognized propagation confidence -- falling back to the strict default", value=raw)
        return DEFAULT_PROPAGATION_MIN_CONFIDENCE


async def drain_tracklists(
    ctx: dict[str, Any],
    *,
    limit: int = DEFAULT_LOOKUP_LIMIT,
    agent_id: str | None = None,
    include_unknown: bool = False,
    flagged_file_ids: Sequence[str] | None = None,
    propagation_min_confidence: str | None = None,
) -> dict[str, Any]:
    """Run one bounded slice of the drain and report what it spent and what it wrote.

    Args:
        ctx: SAQ context; ``ctx["async_session"]`` is the short-session factory the drain opens and
            closes around every phase (never across network I/O -- phaze-1bcc / phaze-igwi).
        limit: maximum lookups this slice may spend. Requests, not files, not seconds.
        agent_id: restrict the corpus to one scanning agent's files.
        include_unknown: also queue sets the classifier could not decide about. Off by default --
            a wrong guess costs a request that cannot be reclaimed, and the undecided tail is
            precisely the population where the classifier has no evidence.
        flagged_file_ids: files the operator wants answered first; their sets sort to the front.
        propagation_min_confidence: a ``DuplicateConfidence`` value. Defaults to ``exact``.

    Returns:
        The :class:`~phaze.services.tracklist_drain.DrainReport` as a JSON-safe mapping.
    """
    gate = _parse_confidence(propagation_min_confidence)
    scraper = TracklistScraper()
    renderer = TracklistRenderer()
    try:
        report = await drain_once(
            ctx["async_session"],
            search=scraper,
            renderer=renderer,
            limit=limit,
            agent_id=agent_id,
            include_unknown=include_unknown,
            flagged_file_ids=_parse_file_ids(flagged_file_ids),
            propagation_min=gate,
        )
    finally:
        # Both closes always run: a renderer left holding a headful Chrome (and its Xvfb display)
        # would leak a process per job, and phaze-fq9h.5 puts that browser inside the worker image
        # where nothing else will reap it.
        await renderer.aclose()
        await scraper.close()

    return report.as_dict()


async def tracklist_drain_status(ctx: dict[str, Any], *, agent_id: str | None = None, include_unknown: bool = False) -> dict[str, Any]:
    """Report the queue and the projected schedule WITHOUT spending a single request.

    Read-only by construction: it builds the same queue :func:`drain_tracklists` would and returns
    the funnel. The admin UI (phaze-fq9h.8) needs "how much is left, and how long will it take"
    far more often than it needs a run, and answering that question must never cost budget.

    ``flagged_queued`` / ``flagged_total`` (phaze-fq9h.8) come from THIS SAME queue build --
    ``build_drain_queue`` already merges the persisted priority flags
    (:func:`phaze.services.tracklist_priority.load_flagged_file_ids`) into every candidate's
    ``flagged`` bit, so counting them here is reading the one status path this function already
    is, not standing up a second one.
    """
    async with ctx["async_session"]() as session:
        queue = await build_drain_queue(session, agent_id=agent_id, include_unknown=include_unknown)
        flagged_total = len(await load_flagged_file_ids(session))

    stats = queue.stats
    return {
        "queued": len(queue.entries),
        "cached": len(queue.cached),
        "unique_sets": stats.unique_sets,
        "candidate_files": stats.candidate_files,
        "collapse_ratio": round(stats.collapse_ratio, 4),
        "projected_days": round(stats.projected_days, 1),
        "days_without_dedup": round(stats.days_without_dedup, 1),
        "cached_positive": stats.cached_positive,
        "cached_negative": stats.cached_negative,
        "cached_backoff": stats.cached_backoff,
        "cached_exhausted": stats.cached_exhausted,
        "next_set_keys": [candidate.set_key for candidate in queue.entries[:10]],
        "flagged_queued": sum(1 for candidate in queue.entries if candidate.flagged),
        "flagged_total": flagged_total,
    }
