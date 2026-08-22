"""Operator priority flags + the per-file lookup review (phaze-fq9h.8).

**ZERO live requests.** Nothing here touches the network, the browser, or 1001Tracklists -- every
case builds its own ``FileMetadata`` and, where a cache row is needed, writes it directly via
``record_outcome`` at the SAME key :func:`get_file_tracklist_review` would compute (the real
``derive_query`` / ``group_unique_sets`` pipeline), never a fabricated one.

Three things this suite is shaped to prove, mirroring the bead's own acceptance criteria:

* the flag PERSISTS (survives past a single call) and is idempotent;
* a scraped tracklist and a propagated one render as genuinely different states, never collapsed;
* a definitive negative (``not_found``) and a transient failure (``blocked`` et al.) stay two
  different states too, and the "not eligible / not yet looked up" case never masquerades as one.

Filenames are invented text describing public events; no archive identifier appears here
(CLAUDE.md's conventions).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.enums.tracklist_candidate import TRANSIENT_MAX_ATTEMPTS, CacheDecision, LookupOutcome
from phaze.models.metadata import FileMetadata
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion
from phaze.services.tracklist_candidates import CandidateSignals, group_unique_sets, set_key
from phaze.services.tracklist_lookup_cache import record_outcome
from phaze.services.tracklist_priority import (
    _signals_for,
    clear_flags,
    flag_file_for_lookup,
    flag_files_for_lookup,
    get_file_tracklist_review,
    is_flagged,
    load_flagged_file_ids,
    unflag_file,
)
from phaze.services.tracklist_query import derive_query


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

LIVE_SET_FILENAME = "Nightwave-Live_At_Late_Signal-2025-03-14-WEB-FLAC-GRVMSTR.mp3"
"""Long-duration file -- classifies LIVE_SET regardless of the filename's own markers."""
TRACK_FILENAME = "Nightwave-Short_Interlude-2025-03-14.mp3"
"""Short-duration file -- classifies TRACK; never enters the drain queue."""


async def _set_key_for(session: AsyncSession, file: FileRecord, *, duration: float | None) -> str:
    """Compute the SAME unique-set key ``get_file_tracklist_review`` would, for seeding a cache row.

    Deliberately calls the real production pipeline (``derive_query`` -> ``group_unique_sets``)
    rather than hand-rolling a key, so a test seeding a cache row and the code under test reading
    it can never silently drift onto two different hashing schemes.
    """
    signals = CandidateSignals(file_id=file.id, filename=file.original_filename, sha256_hash=file.sha256_hash, duration_seconds=duration)
    derived = derive_query(signals.filename)
    signals = replace(signals, derived_query=derived.query)
    unique_sets = group_unique_sets([signals])
    assert unique_sets, "the file must classify as a live set for this helper to make sense"
    return unique_sets[0].key


class TestSignalsFor:
    """phaze-bk9el.28: the pure file -> ``CandidateSignals`` mapping lifted out of
    ``_lookup_status_without_a_tracklist``.

    It carried seven of that function's thirteen decision points and expresses no decision, so it
    is tested here directly -- without a session, which is the point of having separated it.
    """

    def _file(self, **overrides: object):  # type: ignore[no-untyped-def]
        from phaze.models.file import FileRecord

        defaults: dict[str, object] = {
            "id": uuid.uuid4(),
            "original_filename": LIVE_SET_FILENAME,
            "original_filename_repaired": None,
            "sha256_hash": "a" * 64,
            "original_path": f"/archive/{LIVE_SET_FILENAME}",
            "file_type": "mp3",
            "file_size": 123_456,
        }
        return FileRecord(**(defaults | overrides))  # type: ignore[arg-type]

    def test_every_metadata_field_is_none_when_there_is_no_metadata_row(self) -> None:
        """A file with no ``FileMetadata`` row is ordinary, not exceptional -- ``classify`` copes
        with an absent duration, so the mapping must yield None rather than raise."""
        signals = _signals_for(self._file(), None)

        assert signals.duration_seconds is None
        assert signals.bitrate is None
        assert signals.track_number is None
        assert signals.artist is None
        assert signals.title is None
        assert signals.album is None
        assert signals.filename == LIVE_SET_FILENAME

    def test_every_metadata_field_is_carried_through_when_the_row_exists(self) -> None:
        file = self._file()
        metadata = FileMetadata(file_id=file.id, duration=7200.0, bitrate=320, track_number=3, artist="Nightwave", title="Late Signal", album="Live")

        signals = _signals_for(file, metadata)

        assert (signals.duration_seconds, signals.bitrate, signals.track_number) == (7200.0, 320, 3)
        assert (signals.artist, signals.title, signals.album) == ("Nightwave", "Late Signal", "Live")
        assert signals.file_id == file.id
        assert (signals.sha256_hash, signals.original_path, signals.file_type, signals.file_size) == (
            file.sha256_hash,
            file.original_path,
            file.file_type,
            file.file_size,
        )

    def test_the_repaired_filename_wins_when_one_exists(self) -> None:
        """phaze-x4ux: ``original_filename_repaired`` is the best-known-clean text, and the whole
        query-derivation chain downstream reads ``signals.filename``."""
        file = self._file(original_filename="Sven VÃ¤th-Live.mp3", original_filename_repaired="Sven Väth-Live.mp3")

        assert _signals_for(file, None).filename == "Sven Väth-Live.mp3"


class TestFlagPersistence:
    async def test_flag_then_load_returns_the_file(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)

        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()

        assert await load_flagged_file_ids(session) == {file.id}
        assert await is_flagged(session, file.id) is True

    async def test_flagging_twice_is_idempotent_not_a_duplicate_row(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)

        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()
        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()

        assert await load_flagged_file_ids(session) == {file.id}

    async def test_unflag_clears_the_flag(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()

        await unflag_file(session, file.id)
        await session.commit()

        assert await load_flagged_file_ids(session) == set()
        assert await is_flagged(session, file.id) is False

    async def test_unflag_a_never_flagged_file_is_a_noop(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)

        await unflag_file(session, file.id)  # must not raise
        await session.commit()

        assert await is_flagged(session, file.id) is False

    async def test_flag_files_for_lookup_flags_a_whole_set_at_once(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """phaze-vu88k.4: the bulk twin of flag_file_for_lookup, used by refresh_tracklists.

        One upsert must produce exactly what a per-id loop produced -- the point of the bulk form
        is the round trips it saves, not a different result.
        """
        first = await make_file(original_filename=LIVE_SET_FILENAME)
        second = await make_file(original_filename="Another Artist - Live @ Somewhere 2024-05-01.mp3")

        await flag_files_for_lookup(session, [first.id, second.id], now=NOW)
        await session.commit()

        assert await load_flagged_file_ids(session) == {first.id, second.id}
        assert await is_flagged(session, first.id) is True
        assert await is_flagged(session, second.id) is True

    async def test_flag_files_for_lookup_is_idempotent_over_an_already_flagged_file(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """ON CONFLICT DO UPDATE, exactly as the per-id form: re-flagging re-confirms, never raises.

        Mixed input is the real refresh case -- a page whose files were flagged by an earlier
        refresh and a newly-affected one arrive in the same set.
        """
        already = await make_file(original_filename=LIVE_SET_FILENAME)
        fresh = await make_file(original_filename="Another Artist - Live @ Somewhere 2024-05-01.mp3")
        await flag_file_for_lookup(session, already.id, now=NOW)
        await session.commit()

        await flag_files_for_lookup(session, [already.id, fresh.id], now=NOW)
        await session.commit()

        assert await load_flagged_file_ids(session) == {already.id, fresh.id}

    async def test_flag_files_for_lookup_with_no_ids_is_a_noop_not_an_unfiltered_insert(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """Empty input must issue nothing -- an empty VALUES list would be a SQL error, not a no-op."""
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()

        await flag_files_for_lookup(session, [])  # must not raise
        await session.commit()

        assert await load_flagged_file_ids(session) == {file.id}

    async def test_clear_flags_retires_a_whole_set_at_once(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """phaze-2akf: the drain's post-answer sweep, bulk over a unique set's members.

        Load-bearing rather than convenience: a flag on an already-tracklisted file is now the
        REFRESH override, so one left standing after the drain answers the set would re-admit that
        file past the funnel's already-tracklisted filter on every future pass, forever.
        """
        first = await make_file(original_filename=LIVE_SET_FILENAME)
        second = await make_file(original_filename="Another Artist - Live @ Somewhere 2024-05-01.mp3")
        await flag_file_for_lookup(session, first.id, now=NOW)
        await flag_file_for_lookup(session, second.id, now=NOW)
        await session.commit()

        await clear_flags(session, [first.id, second.id])
        await session.commit()

        assert await load_flagged_file_ids(session) == set()

    async def test_clear_flags_with_no_ids_is_a_noop_not_an_unfiltered_delete(
        self,
        session: AsyncSession,
        make_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """An empty member list must not become ``DELETE FROM tracklist_priority_flags``.

        ``in_([])`` happens to be safe in SQLAlchemy, but the early return states the intent where a
        future refactor can see it -- wiping every operator's outstanding request because one set
        had no members is not a failure anyone would notice quickly.
        """
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()

        await clear_flags(session, [])
        await session.commit()

        assert await load_flagged_file_ids(session) == {file.id}


class TestFileTracklistReview:
    async def test_missing_file_returns_none(self, session: AsyncSession) -> None:
        assert await get_file_tracklist_review(session, uuid.uuid4()) is None

    async def test_never_looked_up_and_flagged(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        await flag_file_for_lookup(session, file.id, now=NOW)
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.tracklist is None
        assert review.latest_version is None
        assert review.cache_entry is None
        assert review.flagged is True
        assert review.eligible is True

    async def test_never_looked_up_not_a_live_set_is_ineligible(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=TRACK_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=180.0))
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.eligible is False
        assert review.tracklist is None
        assert review.cache_entry is None

    async def test_an_embedded_tracklist_excludes_the_file_before_classification(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A file with a tag-embedded tracklist never enters the drain funnel (phaze-fq9h.8 bounce).

        Long duration and a set-shaped filename would otherwise classify this LIVE_SET; the
        embedded tracklist must still win, because ``build_queue_from_signals`` excludes it BEFORE
        classification ever runs. If ``eligible`` did not honor this, the record page would offer
        a Prioritize button whose enqueued lookup spends a live request on a completely different,
        unrelated set.
        """
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(
            FileMetadata(
                file_id=file.id,
                duration=7200.0,
                raw_tags={"comment": "00:00 Opener\n05:00 Second track\n10:00 Third track"},
            )
        )
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.answered_elsewhere == "embedded tracklist"
        assert review.eligible is False
        assert review.tracklist is None
        assert review.cache_entry is None

    async def test_a_cue_companion_excludes_the_file_before_classification(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A file with a linked .cue companion never enters the drain funnel either."""
        from phaze.models.file_companion import FileCompanion

        file = await make_file(original_filename=LIVE_SET_FILENAME)
        companion = await make_file(original_filename="companion.cue", file_type="cue")
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        session.add(FileCompanion(companion_id=companion.id, media_id=file.id))
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.answered_elsewhere == "cue companion"
        assert review.eligible is False
        assert review.cache_entry is None

    async def test_a_definitive_negative_cache_entry_is_surfaced_distinctly(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.NOT_FOUND, now=NOW)
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.tracklist is None
        assert review.cache_entry is not None
        assert review.cache_entry.outcome == LookupOutcome.NOT_FOUND.value

    async def test_a_transient_failure_cache_entry_is_surfaced_distinctly_from_not_found(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.BLOCKED, detail="turnstile", now=NOW)
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.tracklist is None
        assert review.cache_entry is not None
        assert review.cache_entry.outcome == LookupOutcome.BLOCKED.value
        assert review.cache_entry.outcome != LookupOutcome.NOT_FOUND.value

    async def test_a_multi_part_members_cluster_row_is_found_by_query_text_when_the_singleton_key_misses(
        self, session: AsyncSession, make_file
    ) -> None:  # type: ignore[no-untyped-def]
        """phaze-3dwsp: part 2 of a two-part set, parked under the CLUSTER's key.

        The drain writes the cache row under ``set_key(cluster_query, cluster_median_duration)``.
        This file's own singleton key -- ``set_key(this file's query, THIS file's duration)`` --
        differs because a linked part's own duration (35 min) lands in a different 5-minute bucket
        than the cluster's median (90 min, dominated by the much longer part 1). Before the fix
        this diverging key was a flat MISS and the record page told the operator the set was
        "never looked up", when it was actually parked awaiting their attention.
        """
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=2100.0))  # this file's own duration: 35 min
        await session.commit()

        # The cluster's query_text is `group_unique_sets`' normalized form of the derived query --
        # the same normalization `get_file_tracklist_review` applies to build its own key, so this
        # mirrors what the drain actually writes rather than a hand-normalized approximation.
        signals = CandidateSignals(file_id=file.id, filename=file.original_filename, sha256_hash=file.sha256_hash, duration_seconds=2100.0)
        derived = derive_query(signals.filename)
        signals = replace(signals, derived_query=derived.query)
        cluster_query_text = group_unique_sets([signals])[0].query_text
        cluster_key = set_key(cluster_query_text, 5400.0)  # the cluster's median duration: 90 min
        for _ in range(TRANSIENT_MAX_ATTEMPTS):
            await record_outcome(session, set_key=cluster_key, query_text=cluster_query_text, outcome=LookupOutcome.BLOCKED, now=datetime.now(UTC))
            await session.commit()

        own_key = await _set_key_for(session, file, duration=2100.0)
        assert own_key != cluster_key, "the singleton and cluster keys must actually diverge for this test to mean anything"

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_entry is not None, "the parked cluster row must be found via the query-text fallback"
        assert review.cache_decision is CacheDecision.TRANSIENT_EXHAUSTED
        assert review.actionable is False

    @pytest.mark.parametrize("propagated", [False, True])
    async def test_a_tracklist_shows_scraped_vs_propagated_distinctly(self, session: AsyncSession, make_file, propagated: bool) -> None:  # type: ignore[no-untyped-def]
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        tracklist = Tracklist(
            external_id="ext-1",
            source_url="https://www.1001tracklists.com/tracklist/ext-1",
            file_id=file.id,
            match_confidence=88,
            propagated_from_set_key="some-set-key" if propagated else None,
            propagation_confidence="exact" if propagated else None,
        )
        session.add(tracklist)
        await session.flush()
        version = TracklistVersion(tracklist_id=tracklist.id, version_number=1)
        session.add(version)
        await session.flush()
        tracklist.latest_version_id = version.id
        session.add(TracklistTrack(version_id=version.id, position=1, artist="Nightwave", title="Opener"))
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.tracklist is not None
        assert review.latest_version is not None
        assert review.latest_version.id == version.id
        assert review.is_propagated is propagated
        assert len(review.tracks) == 1
        assert review.tracks[0].title == "Opener"
        assert review.cache_entry is None


class TestActionable:
    """phaze-z8xq7: ``actionable`` is ``eligible`` AND "the cache verdict would actually be
    queried now" -- the gate the Prioritize route and the honest-suppression template copy both
    read, so a cache-suppressed set can never claim a lookup has been dispatched when the drain's
    own cache decision will keep it out of the queue regardless of the flag.
    """

    async def test_never_looked_up_is_actionable(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """STATE 3b (no cache row at all -- MISS) is unaffected: still actionable."""
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_entry is None
        assert review.eligible is True
        assert review.actionable is True

    async def test_not_a_live_set_is_not_actionable(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """STATE 3b, ineligible by classification: ``actionable`` follows ``eligible`` here too."""
        file = await make_file(original_filename=TRACK_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=180.0))
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.eligible is False
        assert review.actionable is False

    async def test_a_suppressed_negative_within_ttl_is_not_actionable(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """The exact bug scenario: a definitive NOT_FOUND still inside its 180-day TTL.

        ``eligible`` stays True by construction (classification never consults the cache), but
        ``actionable`` must be False -- the drain will not query this set again until the TTL
        elapses, so flagging it is inert.
        """
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.NOT_FOUND, now=NOW)
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_decision is CacheDecision.SUPPRESSED_NEGATIVE
        assert review.eligible is True
        assert review.actionable is False

    async def test_a_negative_past_its_ttl_is_actionable_again(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """Past the TTL the cache reports NEGATIVE_EXPIRED -- re-queryable, so actionable again."""
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.NOT_FOUND, now=NOW, negative_ttl_days=0)
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_decision is CacheDecision.NEGATIVE_EXPIRED
        assert review.actionable is True

    async def test_a_transient_failure_in_backoff_is_not_actionable(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """A transient failure still inside its short backoff window: not actionable YET (it will
        retry automatically once the backoff elapses -- distinct from the negative-TTL case)."""
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        # Backoff is computed relative to the WRITE time, and the review's own cache read always
        # uses the real wall clock (no injectable `now`) -- so this must be freshly-written, not
        # the module's fixed historical NOW, or the 30-minute window would already have elapsed.
        await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.BLOCKED, now=datetime.now(UTC))
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_decision is CacheDecision.BACKOFF
        assert review.actionable is False

    async def test_a_transient_failure_ready_for_retry_is_actionable(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """Past its backoff window, a transient failure is TRANSIENT_RETRY_READY -- actionable."""
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.BLOCKED, now=NOW)
        await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_decision is CacheDecision.TRANSIENT_RETRY_READY
        assert review.actionable is True

    async def test_a_parked_transient_exhausted_set_is_not_actionable(self, session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
        """After TRANSIENT_MAX_ATTEMPTS consecutive transient failures the set is PARKED -- worse
        than the TTL case, since nothing resets it automatically. Never actionable."""
        file = await make_file(original_filename=LIVE_SET_FILENAME)
        session.add(FileMetadata(file_id=file.id, duration=7200.0))
        await session.commit()
        key = await _set_key_for(session, file, duration=7200.0)
        for _ in range(TRANSIENT_MAX_ATTEMPTS):
            await record_outcome(session, set_key=key, query_text="nightwave late signal", outcome=LookupOutcome.BLOCKED, now=datetime.now(UTC))
            await session.commit()

        review = await get_file_tracklist_review(session, file.id)

        assert review is not None
        assert review.cache_decision is CacheDecision.TRANSIENT_EXHAUSTED
        assert review.actionable is False
