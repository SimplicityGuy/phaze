"""The on-demand tracklist REFRESH sweep (phaze-2akf). No network, ever.

WHAT THIS MODULE REPLACED
-------------------------
It used to be ~1,400 lines covering ``search_tracklist`` / ``scrape_and_store_tracklist`` and the
monthly ``refresh_tracklists`` cron. All three are retired. The behaviours those tests protected
were each earned by a past defect, so none of them was allowed to simply evaporate; each moved to
the drain, and the table below says where -- because "we deleted the tests with the code" is only
honest if the invariant is still asserted somewhere.

============  =====================================  =========================================
bead          invariant                              where it lives now
============  =====================================  =========================================
phaze-1bcc    no DB session held across network I/O,  ``services/tracklist_drain.drain_once``
              and stores sorted by external_id so     opens a SHORT session per phase, with the
              two searches cannot ABBA-deadlock on    search and the render outside all of them.
              the per-external_id advisory locks      Each candidate takes its own advisory lock
                                                      in its OWN one-candidate transaction, so
                                                      only one lock is ever held and there is no
                                                      ordering to get wrong
                                                      (``tests/identify/services/test_tracklist_drain.py``)
phaze-gfyr /  never overwrite good tracklist data     structural now: ``perform_lookup`` records
phaze-g2j3    with an empty version, caught           a track-less render as ``PARSE_FAILED`` and
              PER ITEM so one bad result cannot       ``_append_version`` is never reached, so an
              roll back its siblings                  empty version cannot be written at all.
                                                      Per-candidate commits replace the per-item
                                                      catch: one bad lookup cannot roll back
                                                      another because they are not in the same
                                                      transaction
phaze-4a5w    never steal a tracklist already         ``_resolve_canonical`` -- same rule, same
              linked to a DIFFERENT file              warning log, asserted in the drain suite
phaze-rkxy    an auto-link needs a CONFIRMED          ``tracklist_result_scorer.select_result``:
              same-window date                       a date mismatch DISQUALIFIES the row rather
                                                      than capping it below a threshold, and an
                                                      ambiguous date refuses transiently
phaze-x4ux    mojibake repaired ONCE at the ingest    the file side is repaired in
              boundary, because tracklists.           ``candidate_signals_query`` (COALESCE over
              search_vector is GENERATED over         ``original_filename_repaired``); the
              artist/event                           tracklist side now stores the SEARCH ROW's
                                                      own artist/event, i.e. the site's text
                                                      rather than our re-encoding of it
phaze-hu8v    never re-fetch what we have, and a      ``tracklist_lookup_cache`` -- and the
              ZERO-TRACK first scrape must not        poisoning case is impossible rather than
              count as cached                        guarded: a zero-track lookup is never
                                                      recorded as ``FOUND`` in the first place
phaze-fq9h.7  every ``external_id`` read filters on   preserved in ``_resolve_targets`` below and
              ``propagated_from_set_key IS NULL``     throughout the drain
============  =====================================  =========================================

WHAT IS ASSERTED HERE
---------------------
The refresh task's own contract: it refuses an unbounded sweep, it spends no host requests, it
drops only the POSITIVE cache row, it flags every file the page serves rather than just the
canonical one, and it never acts on a row it cannot re-read.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import select

from phaze.enums.tracklist_candidate import LookupOutcome
from phaze.models.file import FileRecord
from phaze.models.tracklist import Tracklist
from phaze.models.tracklist_lookup_cache import TracklistLookupCache
from phaze.models.tracklist_priority_flag import TracklistPriorityFlag
from phaze.services.tracklist_lookup_cache import record_outcome
from phaze.tasks import tracklist as task_module
from phaze.tasks.controller import settings as controller_settings
from phaze.tasks.tracklist import _parse_uuids, _resolve_targets, refresh_tracklists


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


pytestmark = pytest.mark.asyncio


def ctx_for(session: AsyncSession) -> dict[str, Any]:
    """A SAQ ctx whose ``async_session`` hands back the test's savepoint-nested session."""

    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return {"async_session": _factory}


async def _seed_file(session: AsyncSession, name: str = "set.mp3") -> uuid.UUID:
    file_id = uuid.uuid4()
    session.add(
        FileRecord(
            id=file_id,
            agent_id="test-fileserver",
            sha256_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            original_path=f"/music/{uuid.uuid4().hex}/{name}",
            original_filename=name,
            current_path=f"/music/{name}",
            file_type="mp3",
            file_size=1_000_000,
        )
    )
    await session.flush()
    return file_id


async def _seed_tracklist(
    session: AsyncSession,
    *,
    external_id: str,
    file_id: uuid.UUID | None = None,
    set_key: str | None = None,
    source: str = "1001tracklists",
    source_url: str = "https://www.1001tracklists.com/tracklist/abc123/x.html",
) -> Tracklist:
    row = Tracklist(
        id=uuid.uuid4(),
        external_id=external_id,
        source_url=source_url,
        file_id=file_id,
        source=source,
        propagated_from_set_key=set_key,
    )
    session.add(row)
    await session.flush()
    return row


async def _flagged_file_ids(session: AsyncSession) -> set[uuid.UUID]:
    return set((await session.execute(select(TracklistPriorityFlag.file_id))).scalars().all())


async def _cache_rows(session: AsyncSession, external_id: str) -> list[TracklistLookupCache]:
    result = await session.execute(select(TracklistLookupCache).where(TracklistLookupCache.external_id == external_id))
    return list(result.scalars().all())


class TestArgumentParsing:
    async def test_unparseable_ids_are_dropped_not_raised(self) -> None:
        """A typo in an operator's selection must not fail the whole sweep (mirrors the drain)."""
        good = uuid.uuid4()
        assert _parse_uuids([str(good), "not-a-uuid", ""]) == [good]
        assert _parse_uuids(None) == []


class TestResolveTargets:
    async def test_no_ids_at_all_selects_nothing_rather_than_everything(self, session: AsyncSession) -> None:
        """The guard that makes an unfiltered ``or_()`` impossible.

        ``or_()`` with no clauses evaluates to TRUE in SQLAlchemy, which would turn "the caller
        named nothing" into "every canonical tracklist in the corpus" -- the corpus-wide sweep this
        whole task is built to refuse. The early return is what stops that, and it is asserted here
        directly because the task-level guard above would otherwise hide it forever.
        """
        await _seed_tracklist(session, external_id="not-a-target")
        assert await _resolve_targets(session, [], []) == []


class TestRefusesAnUnboundedSweep:
    """The single most important property: refresh is on-demand, not a policy.

    The retired cron swept every tracklist older than 90 days on a schedule. Defaulting to
    "everything" when called with no arguments would rebuild that policy behind an
    on-demand-looking signature -- against a host whose entire published budget is ~1 request / 8 s.
    """

    async def test_no_targets_is_a_refusal_that_writes_nothing(self, session: AsyncSession) -> None:
        tracklist = await _seed_tracklist(session, external_id="untouched-1")
        await record_outcome(session, set_key="k1", query_text="q", outcome=LookupOutcome.FOUND, external_id=tracklist.external_id)

        result = await refresh_tracklists(ctx_for(session))

        assert result["status"] == "no_targets"
        assert result["refreshed"] == 0
        assert result["files_flagged"] == 0
        assert len(await _cache_rows(session, "untouched-1")) == 1
        assert await _flagged_file_ids(session) == set()

    async def test_empty_lists_are_also_a_refusal(self, session: AsyncSession) -> None:
        """``tracklist_ids=[]`` is "the operator selected nothing", not "select everything"."""
        assert (await refresh_tracklists(ctx_for(session), tracklist_ids=[], file_ids=[]))["status"] == "no_targets"


class TestRearmsTheDrain:
    async def test_drops_the_positive_cache_row_and_flags_the_file(self, session: AsyncSession) -> None:
        """The two halves of "ask again": forget the answer, and re-admit the file.

        Both are required. Dropping the cache alone leaves the file filtered out of the funnel as
        already-tracklisted; flagging alone leaves the cache answering FOUND, which keeps the set
        in ``cached`` -- where it costs nothing and does nothing.
        """
        file_id = await _seed_file(session)
        tracklist = await _seed_tracklist(session, external_id="page-1", file_id=file_id)
        await record_outcome(session, set_key="set-1", query_text="q", outcome=LookupOutcome.FOUND, external_id="page-1")

        result = await refresh_tracklists(ctx_for(session), tracklist_ids=[str(tracklist.id)])

        assert result["status"] == "rearmed"
        assert result["refreshed"] == 1
        assert result["cache_entries_cleared"] == 1
        assert await _cache_rows(session, "page-1") == []
        assert await _flagged_file_ids(session) == {file_id}

    async def test_the_task_module_cannot_reach_the_network(self) -> None:
        """Refresh spends no host requests -- asserted structurally, not by mocking.

        The module's namespace holds no HTTP client, no scraper, no renderer and no rate-limiter.
        If one appears, this fails for the right reason: a refresh that fetches is a SECOND
        consumer of the whole-host budget, which is precisely what the retired cron was.
        """
        forbidden = {"httpx", "TracklistScraper", "TracklistRenderer", "reserve_host_request_slot", "drain_once"}
        assert forbidden.isdisjoint(vars(task_module))

    async def test_flags_every_file_the_page_serves_not_just_the_canonical_one(self, session: AsyncSession) -> None:
        """A propagated projection's file is re-armed too (phaze-fq9h.7).

        Propagation is re-derived from the unique set on the next lookup. Flagging only the
        canonical file would refresh the page while leaving its duplicates pointing at whatever the
        previous pass wrote.
        """
        canonical_file = await _seed_file(session, "canonical.mp3")
        duplicate_file = await _seed_file(session, "duplicate.mp3")
        canonical = await _seed_tracklist(session, external_id="page-3", file_id=canonical_file)
        await _seed_tracklist(session, external_id="page-3", file_id=duplicate_file, set_key="set-3")

        result = await refresh_tracklists(ctx_for(session), tracklist_ids=[str(canonical.id)])

        assert result["files_flagged"] == 2
        assert await _flagged_file_ids(session) == {canonical_file, duplicate_file}

    async def test_addressable_by_file_id_from_the_record_page(self, session: AsyncSession) -> None:
        """The record page knows a file, not a tracklist id -- both spellings reach the same row."""
        file_id = await _seed_file(session)
        await _seed_tracklist(session, external_id="page-4", file_id=file_id)

        assert (await refresh_tracklists(ctx_for(session), file_ids=[str(file_id)]))["refreshed"] == 1
        assert await _flagged_file_ids(session) == {file_id}

    async def test_addressable_by_a_duplicates_file_id_not_just_the_canonicals(self, session: AsyncSession) -> None:
        """phaze-vtovq: naming a DUPLICATE's file id must resolve the CANONICAL row, not nothing.

        A duplicate file's own ``Tracklist`` row is itself a propagated projection --
        ``file_id`` is the duplicate's, ``propagated_from_set_key`` is set -- so the record
        page's "Refresh from 1001Tracklists" button (which renders for any file with a tracklist,
        including projections) named the projection's file id. Before the fix, ANDing the
        canonical filter directly onto that file-id clause matched nothing: ``refreshed`` stayed
        0, no cache row was cleared, no file was flagged, and the button silently did nothing.
        """
        canonical_file = await _seed_file(session, "canonical.mp3")
        duplicate_file = await _seed_file(session, "duplicate.mp3")
        await _seed_tracklist(session, external_id="page-9", file_id=canonical_file)
        await _seed_tracklist(session, external_id="page-9", file_id=duplicate_file, set_key="set-9")
        await record_outcome(session, set_key="set-9", query_text="q", outcome=LookupOutcome.FOUND, external_id="page-9")

        result = await refresh_tracklists(ctx_for(session), file_ids=[str(duplicate_file)])

        assert result["refreshed"] == 1
        assert result["cache_entries_cleared"] == 1
        assert await _cache_rows(session, "page-9") == []
        assert await _flagged_file_ids(session) == {canonical_file, duplicate_file}

    async def test_a_propagated_row_named_directly_is_not_treated_as_canonical(self, session: AsyncSession) -> None:
        """phaze-fq9h.7: only canonical rows are refresh targets.

        Refreshing a projection directly would either re-fetch the same page once per duplicate or
        leave the canonical row untouched. ``_resolve_targets`` filters on
        ``propagated_from_set_key IS NULL``, so a projection named by id resolves to nothing.
        """
        file_id = await _seed_file(session)
        projection = await _seed_tracklist(session, external_id="page-5", file_id=file_id, set_key="set-5")

        result = await refresh_tracklists(ctx_for(session), tracklist_ids=[str(projection.id)])

        assert result["refreshed"] == 0
        assert await _flagged_file_ids(session) == set()


class TestLeavesAloneWhatItCannotReRead:
    async def test_skips_a_row_with_no_source_url(self, session: AsyncSession) -> None:
        """ "Re-read the page" is meaningless with no page (phaze-p1vy's surviving rows)."""
        file_id = await _seed_file(session)
        tracklist = await _seed_tracklist(session, external_id="page-6", file_id=file_id, source_url="")

        result = await refresh_tracklists(ctx_for(session), tracklist_ids=[str(tracklist.id)])

        assert result["refreshed"] == 0
        assert result["skipped"] == 1
        assert await _flagged_file_ids(session) == set()

    async def test_skips_a_non_1001tracklists_source(self, session: AsyncSession) -> None:
        """The allowlist is positive, so retired-source rows stay inert whether or not they persist."""
        file_id = await _seed_file(session)
        tracklist = await _seed_tracklist(session, external_id="page-7", file_id=file_id, source="fingerprint")

        result = await refresh_tracklists(ctx_for(session), tracklist_ids=[str(tracklist.id)])

        assert result["refreshed"] == 0
        assert result["skipped"] == 1

    async def test_leaves_a_negative_or_transient_cache_row_alone(self, session: AsyncSession) -> None:
        """Only the POSITIVE answer is dropped.

        A ``not_found`` row is a fact about the world under its own TTL, and a transient row carries
        the ``attempts`` count that eventually parks a pathological set. Deleting either would
        restart a retry loop the cache exists to bound -- and neither is what "this page changed" is
        a statement about.
        """
        file_id = await _seed_file(session)
        await _seed_tracklist(session, external_id="page-8", file_id=file_id)
        await record_outcome(session, set_key="set-8", query_text="q", outcome=LookupOutcome.NOT_FOUND, external_id="page-8")

        result = await refresh_tracklists(ctx_for(session), file_ids=[str(file_id)])

        assert result["cache_entries_cleared"] == 0
        assert len(await _cache_rows(session, "page-8")) == 1


class TestRegistration:
    async def test_registered_as_an_enqueueable_function(self) -> None:
        assert refresh_tracklists in controller_settings["functions"]

    async def test_has_no_cron_job(self) -> None:
        """Operator decision (2026-08-03): refresh is on-demand and is NEVER scheduled.

        A cron here would re-open exactly the contradiction the drain's cache was built to close --
        "a published tracklist does not change, so never re-fetch" -- as an unbounded background
        consumer of a shared public host's published budget.
        """
        scheduled = {getattr(job.function, "__name__", str(job.function)) for job in controller_settings["cron_jobs"]}
        assert "refresh_tracklists" not in scheduled

    async def test_the_retired_tasks_are_not_registered_anywhere(self) -> None:
        """The legacy pair must not come back through the registry (phaze-2akf)."""
        registered = {getattr(fn, "__name__", str(fn)) for fn in controller_settings["functions"]}
        assert "search_tracklist" not in registered
        assert "scrape_and_store_tracklist" not in registered
