"""Tests for the release-group date-order learner (phaze-5fta.3).

The pure-classification tests need no database. The full-refresh / idempotency / atomicity tests
use the ``session`` fixture (real Postgres) through a drain-shaped session factory, because the
learner deliberately opens one short session per keyset page and one more for the write
transaction -- that shape is the thing under test, not an implementation detail.

FIXTURE NAMING: every filename here is INVENTED to show a shape, and every release-group tag is a
made-up token (``alphagrp`` / ``betagrp`` / ...), per the repo's no-local-identifiers convention.
Where the epic's measured groups matter they are referred to in prose, never used as fixture data.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from phaze.models.filename_convention import FilenameConvention
from phaze.services.filename_convention_learner import (
    CONVENTION_KIND,
    CONVENTION_SCOPE,
    DateOrder,
    DateVerdict,
    GroupTally,
    LearnerReport,
    learn_release_group_date_conventions,
    read_date_order,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy.ext.asyncio import AsyncSession


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Date classification -- pure, no DB
# --------------------------------------------------------------------------------------------


class TestReadDateOrder:
    """The self-resolving / ambiguous split the whole epic rests on."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            # second component > 12 -> it can only be a day -> MM-DD
            ("Artist - Event - 04-25-2014 - sbd-alphagrp.mp3", DateOrder.MONTH_FIRST),
            # first component > 12 -> it can only be a day -> DD-MM
            ("Artist - Event - 25-04-2014 - sbd-alphagrp.mp3", DateOrder.DAY_FIRST),
        ],
    )
    def test_a_component_that_cannot_be_a_month_resolves_the_order(self, filename: str, expected: DateOrder) -> None:
        assert read_date_order(filename).order is expected

    def test_both_components_under_thirteen_is_ambiguous(self) -> None:
        """The 35.2% of the corpus this epic exists to resolve: both readings are real dates."""
        reading = read_date_order("Artist - Event - 04-05-2014 - sbd-alphagrp.mp3")
        assert reading.verdict is DateVerdict.AMBIGUOUS
        assert reading.order is None
        assert reading.raw == "04-05-2014"

    def test_no_date_at_all_is_none_present_not_an_error(self) -> None:
        assert read_date_order("Artist - Event - sbd-alphagrp.mp3").verdict is DateVerdict.NONE_PRESENT

    @pytest.mark.parametrize(
        "filename",
        [
            "Artist - Event - 2014.04.25 - sbd-alphagrp.mp3",  # YYYY.MM.DD
            "Artist - Event - 2014 - sbd-alphagrp.mp3",  # bare year
            "Artist - Event - 25 April 2014 - sbd-alphagrp.mp3",  # month name
            "Artist - Event - 2014-04-25 - sbd-alphagrp.mp3",  # ISO
        ],
    )
    def test_out_of_scope_date_shapes_are_not_read(self, filename: str) -> None:
        """Explicitly out of scope for this learner -- and explicitly NOT an error (phaze-5fta.3)."""
        assert read_date_order(filename).verdict is DateVerdict.NONE_PRESENT

    @pytest.mark.parametrize(
        "filename",
        [
            "Artist - Event - 00-13-2014 - sbd-alphagrp.mp3",  # neither component is a month
            "Artist - Event - 32-05-2014 - sbd-alphagrp.mp3",  # 32 is no day either
            "Artist - Event - 29-02-2014 - sbd-alphagrp.mp3",  # February 29 does not exist in 2014
            "Artist - Event - 31-04-2014 - sbd-alphagrp.mp3",  # April has 30 days
        ],
    )
    def test_shape_matching_non_dates_are_rejected_by_the_calendar(self, filename: str) -> None:
        """A range check would have counted every one of these as self-resolving EVIDENCE.

        ``29-02-2014`` is the sharpest: ``29 > 12`` so a naive "> 12 must be a day" rule reads it as
        a confident DD-MM vote, when February 29 simply does not exist in 2014 and the string is not
        a date at all.
        """
        assert read_date_order(filename).verdict is DateVerdict.NOT_A_DATE

    def test_two_different_date_tokens_yield_no_evidence(self) -> None:
        reading = read_date_order("Artist - 04-05-2014 - rip 11-12-2015 - sbd-alphagrp.mp3")
        assert reading.verdict is DateVerdict.MULTIPLE

    def test_the_same_token_repeated_is_one_date(self) -> None:
        reading = read_date_order("04-25-2014/Artist - Event - 04-25-2014 - sbd-alphagrp.mp3")
        assert reading.order is DateOrder.MONTH_FIRST

    def test_a_longer_digit_run_is_not_a_date_token(self) -> None:
        """The lookarounds: without them an offset match would invent a date the string lacks."""
        assert read_date_order("Artist - 123-45-20141 - sbd-alphagrp.mp3").verdict is DateVerdict.NONE_PRESENT

    def test_as_date_composes_the_calendar_date_for_the_given_order(self) -> None:
        reading = read_date_order("Artist - Event - 04-05-2014 - sbd-alphagrp.mp3")
        assert reading.as_date(DateOrder.DAY_FIRST) is not None
        assert reading.as_date(DateOrder.DAY_FIRST).isoformat() == "2014-05-04"  # type: ignore[union-attr]
        assert reading.as_date(DateOrder.MONTH_FIRST).isoformat() == "2014-04-05"  # type: ignore[union-attr]

    def test_as_date_is_none_when_there_was_no_token(self) -> None:
        assert read_date_order("Artist - Event - sbd-alphagrp.mp3").as_date(DateOrder.MONTH_FIRST) is None

    def test_read_date_order_never_raises(self) -> None:
        for value in ("", "-", "00-00-0000", "\x00", "-".join(["99"] * 40)):
            assert read_date_order(value).verdict in set(DateVerdict)


# --------------------------------------------------------------------------------------------
# Per-group evidence reduction -- pure, no DB
# --------------------------------------------------------------------------------------------


class TestGroupTally:
    def test_majority_wins_and_the_minority_is_the_contradicting_count(self) -> None:
        tally = GroupTally(day_first=3, month_first=97, ambiguous=40)
        assert tally.evidence() == (DateOrder.MONTH_FIRST, 97, 3)

    def test_a_tie_supports_nothing_and_counts_every_file_as_contradicting(self) -> None:
        """A 20-20 split is a real outcome with a nullable convention_value, not all-zeros."""
        assert GroupTally(day_first=20, month_first=20, ambiguous=5).evidence() == (None, 0, 40)

    def test_an_ambiguous_only_group_still_records_the_question(self) -> None:
        assert GroupTally(ambiguous=12).evidence() == (None, 0, 0)


# --------------------------------------------------------------------------------------------
# Full refresh against real Postgres
# --------------------------------------------------------------------------------------------


def session_factory_for(session: AsyncSession) -> Callable[[], Any]:
    """A learner-shaped session factory handing back the test's savepoint-nested session.

    The learner opens a fresh short session per keyset page and one more for the write
    transaction; the harness runs everything inside one rolled-back outer transaction. This
    adapter reconciles the two -- the learner's ``commit()`` releases a savepoint, so its writes
    are visible to the assertions and still discarded at teardown.
    """

    @contextlib.asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return _factory


async def _conventions(session: AsyncSession) -> dict[str, FilenameConvention]:
    rows = (
        await session.execute(
            select(FilenameConvention).where(
                FilenameConvention.scope == CONVENTION_SCOPE,
                FilenameConvention.convention_kind == CONVENTION_KIND,
            )
        )
    ).scalars()
    return {row.scope_value: row for row in rows}


class TestFullRefresh:
    async def test_unanimous_group_gets_its_convention_with_the_ambiguous_payoff_sized(self, session: AsyncSession, make_file: Any) -> None:
        """The measurement's shape, at test scale: self-resolving files answer the ambiguous ones.

        Five files whose second component can only be a day vote MM-DD; three individually
        ambiguous files in the same group are counted as payoff, never as votes.
        """
        for day in (13, 14, 15, 16, 17):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")
        for day in (1, 2, 3):
            await make_file(original_filename=f"Artist - Event - 04-0{day}-2014 - sbd-alphagrp.mp3")

        report = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        row = (await _conventions(session))["alphagrp"]
        assert row.convention_value == "MM-DD"
        assert (row.supporting_count, row.contradicting_count) == (5, 0)
        assert row.ambiguous_count == 3
        assert row.confidence == pytest.approx(1.0)
        assert row.computed_at == NOW
        assert (report.self_resolving, report.ambiguous) == (5, 3)
        assert report.groups_written == 1

    async def test_ambiguous_count_never_moves_confidence(self, session: AsyncSession, make_file: Any) -> None:
        """Payoff sizing is not evidence -- the DB's generated column enforces it; assert it holds."""
        await make_file(original_filename="Artist - Event - 04-13-2014 - sbd-alphagrp.mp3")
        for day in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            await make_file(original_filename=f"Artist - Event - 04-0{day}-2014 - sbd-alphagrp.mp3")

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        row = (await _conventions(session))["alphagrp"]
        assert (row.supporting_count, row.contradicting_count, row.ambiguous_count) == (1, 0, 9)
        assert row.confidence == pytest.approx(1.0)

    async def test_two_groups_learn_opposite_conventions_independently(self, session: AsyncSession, make_file: Any) -> None:
        for day in (13, 14, 15):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")
            await make_file(original_filename=f"Artist - Event - {day}-04-2014 - sbd-betagrp.mp3")

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        rows = await _conventions(session)
        assert rows["alphagrp"].convention_value == "MM-DD"
        assert rows["betagrp"].convention_value == "DD-MM"

    async def test_a_contradicting_file_is_counted_and_logged_with_its_filename(
        self, session: AsyncSession, make_file: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The signal the epic refuses to let anyone swallow: it lands in BOTH the count and the log."""
        for day in (13, 14, 15):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")
        odd_one_out = "Artist - Other Event - 22-04-2014 - sbd-alphagrp.mp3"
        await make_file(original_filename=odd_one_out)

        with caplog.at_level("WARNING"):
            report = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        row = (await _conventions(session))["alphagrp"]
        assert row.convention_value == "MM-DD"
        assert (row.supporting_count, row.contradicting_count) == (3, 1)
        assert row.confidence == pytest.approx(0.75)
        assert report.contradictions == 1
        assert odd_one_out in caplog.text

    async def test_every_contradiction_is_logged_not_just_the_first(self, session: AsyncSession, make_file: Any, caplog) -> None:  # type: ignore[no-untyped-def]
        """No per-group cap -- a cap IS the silent drop the precedence rule forbids."""
        for day in (13, 14, 15, 16, 17):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")
        contradicting = [f"Artist - Other - {day}-04-2014 - sbd-alphagrp.mp3" for day in (20, 21, 22)]
        for name in contradicting:
            await make_file(original_filename=name)

        with caplog.at_level("WARNING"):
            report = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        assert report.contradictions == 3
        for name in contradicting:
            assert name in caplog.text

    async def test_a_tied_group_writes_a_null_convention_value(self, session: AsyncSession, make_file: Any) -> None:
        await make_file(original_filename="Artist - Event - 04-13-2014 - sbd-alphagrp.mp3")
        await make_file(original_filename="Artist - Event - 13-04-2014 - sbd-alphagrp.mp3")

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        row = (await _conventions(session))["alphagrp"]
        assert row.convention_value is None
        assert (row.supporting_count, row.contradicting_count) == (0, 2)
        assert row.confidence == pytest.approx(0.0)

    async def test_rerunning_replaces_rather_than_duplicating(self, session: AsyncSession, make_file: Any) -> None:
        """Full-refresh idempotency: a second run over an unchanged corpus reproduces the same rows."""
        for day in (13, 14, 15):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")

        first = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)
        second = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        rows = await _conventions(session)
        assert len(rows) == 1
        assert rows["alphagrp"].supporting_count == 3
        assert first.rows_deleted == 0
        assert second.rows_deleted == 1

    async def test_a_group_that_leaves_the_corpus_leaves_the_store(self, session: AsyncSession, make_file: Any) -> None:
        """Full REFRESH, not merge: last run's rows are not carried forward."""
        stale = FilenameConvention(
            scope=CONVENTION_SCOPE,
            scope_value="gonegrp",
            convention_kind=CONVENTION_KIND,
            convention_value="DD-MM",
            supporting_count=99,
            contradicting_count=0,
            ambiguous_count=0,
        )
        session.add(stale)
        await session.commit()
        await make_file(original_filename="Artist - Event - 04-13-2014 - sbd-alphagrp.mp3")

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        assert set(await _conventions(session)) == {"alphagrp"}

    async def test_another_convention_kind_under_the_same_scope_survives(self, session: AsyncSession, make_file: Any) -> None:
        """The delete is scoped to (scope, convention_kind) -- a future learner's rows are not collateral."""
        other = FilenameConvention(
            scope=CONVENTION_SCOPE,
            scope_value="alphagrp",
            convention_kind="separator_style",
            convention_value="hyphen",
            supporting_count=7,
            contradicting_count=0,
            ambiguous_count=0,
        )
        session.add(other)
        await session.commit()
        await make_file(original_filename="Artist - Event - 04-13-2014 - sbd-alphagrp.mp3")

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        survivor = (await session.execute(select(FilenameConvention).where(FilenameConvention.convention_kind == "separator_style"))).scalar_one()
        assert survivor.supporting_count == 7

    async def test_files_without_a_claimable_group_are_counted_by_reason_not_dropped(self, session: AsyncSession, make_file: Any) -> None:
        await make_file(original_filename="Artist - Event - 04-13-2014.mp3")  # trailing token is the date
        await make_file(original_filename="Artist - Event - 04-14-2014 - sbd-alphagrp.mp3")

        report = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        assert report.files_with_date == 2
        assert report.skipped_no_release_group == 1
        assert sum(report.rejection_reasons.values()) == 1
        assert set(await _conventions(session)) == {"alphagrp"}

    async def test_out_of_scope_shapes_are_swept_past_without_crashing(self, session: AsyncSession, make_file: Any) -> None:
        """The sweep must survive the whole heterogeneous archive, not just its scene half."""
        for name in (
            "Artist - Event - 2014.04.25 - sbd-alphagrp.mp3",
            "Artist - Event - 2014 - sbd-alphagrp.mp3",
            "Artist - Event - 25 April 2014 - sbd-alphagrp.mp3",
            "Some Album/01 Track.mp3",
            "Artist - Event - 00-13-2014 - sbd-alphagrp.mp3",
            "Artist - 04-05-2014 - rip 11-12-2015 - sbd-alphagrp.mp3",
        ):
            await make_file(original_filename=name)
        await make_file(original_filename="Artist - Event - 04-13-2014 - sbd-alphagrp.mp3")

        report = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        assert report.files_scanned == 7
        assert report.skipped_no_date == 4
        assert report.skipped_not_a_date == 1
        assert report.skipped_multiple_dates == 1
        assert (await _conventions(session))["alphagrp"].supporting_count == 1

    async def test_an_empty_corpus_writes_nothing_and_clears_the_store(self, session: AsyncSession) -> None:
        report = await learn_release_group_date_conventions(session_factory_for(session), now=NOW)
        assert report.groups_written == 0
        assert await _conventions(session) == {}

    async def test_keyset_paging_reads_the_whole_corpus(self, session: AsyncSession, make_file: Any) -> None:
        """batch_size=1 forces every page boundary; the tally must be identical to a single page."""
        for day in (13, 14, 15, 16, 17):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")

        report = await learn_release_group_date_conventions(session_factory_for(session), batch_size=1, now=NOW)

        assert report.files_scanned == 5
        assert (await _conventions(session))["alphagrp"].supporting_count == 5

    async def test_the_repaired_filename_is_preferred_over_the_raw_one(self, session: AsyncSession, make_file: Any) -> None:
        """Mojibake in the tail would file a group's evidence under a garbled key (phaze-x4ux)."""
        record = await make_file(original_filename="Artist - Event - 04-13-2014 - sbd-alphagrpÃ©.mp3")
        record.original_filename_repaired = "Artist - Event - 04-13-2014 - sbd-alphagrp.mp3"
        await session.commit()

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        assert set(await _conventions(session)) == {"alphagrp"}

    async def test_the_learner_never_writes_the_generated_confidence_column(self, session: AsyncSession, make_file: Any) -> None:
        """Postgres would REJECT the statement outright; a green write is the proof it never tried."""
        for day in (13, 14, 15, 16):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")
        await make_file(original_filename="Artist - Other - 22-04-2014 - sbd-alphagrp.mp3")

        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        row = (await _conventions(session))["alphagrp"]
        assert row.confidence == pytest.approx(4 / 5)

    async def test_a_failure_mid_sweep_leaves_the_previous_generation_intact(
        self, session: AsyncSession, make_file: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad run is DISCARDED, not half-applied: the write transaction is never reached."""
        for day in (13, 14, 15):
            await make_file(original_filename=f"Artist - Event - 04-{day}-2014 - sbd-alphagrp.mp3")
        await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        def _boom(_name: str) -> None:
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr("phaze.services.filename_convention_learner.read_date_order", _boom)
        with pytest.raises(RuntimeError, match="sweep exploded"):
            await learn_release_group_date_conventions(session_factory_for(session), now=NOW)

        row = (await _conventions(session))["alphagrp"]
        assert row.supporting_count == 3


class TestLearnerReport:
    def test_as_dict_is_json_safe_and_sorts_the_reason_breakdown(self) -> None:
        report = LearnerReport(files_scanned=3, computed_at=NOW)
        report.rejection_reasons["numeric"] += 2
        report.rejection_reasons["empty"] += 1

        payload = report.as_dict()

        assert payload["files_scanned"] == 3
        assert payload["computed_at"] == NOW.isoformat()
        assert list(payload["rejection_reasons"]) == ["empty", "numeric"]

    def test_as_dict_tolerates_a_report_that_never_ran(self) -> None:
        assert LearnerReport().as_dict()["computed_at"] is None
