"""Tests for the gated convention-derived date fallback (phaze-5fta.4).

Four paths, all covered here and in ``tests/review/tasks/test_proposal_date_convention.py``:

1. flag OFF -> nothing happens at all (no query, no context key, no stored key);
2. a self-resolving date -> resolved from the filename, store never consulted;
3. an ambiguous date in an above-bar group -> resolved from the convention, provenance persisted;
4. an ambiguous date in a below-bar group -> left unresolved.

Release-group tags and filenames in the fixtures are invented, per the repo's
no-local-identifiers convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

import pytest

from phaze.models.filename_convention import FilenameConvention
from phaze.services.date_convention import (
    CONTEXT_KEY,
    SOURCE_CONVENTION,
    SOURCE_FILENAME,
    DateProvenance,
    annotate_date_conventions,
    load_release_group_conventions,
    resolve_date,
)
from phaze.services.filename_convention_learner import CONVENTION_KIND, CONVENTION_SCOPE


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


COMPUTED_AT = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

AMBIGUOUS = "Artist - Event - 04-05-2014 - sbd-alphagrp.mp3"
SELF_RESOLVING_MONTH_FIRST = "Artist - Event - 04-25-2014 - sbd-alphagrp.mp3"
SELF_RESOLVING_DAY_FIRST = "Artist - Event - 25-04-2014 - sbd-alphagrp.mp3"
NO_GROUP = "Artist - Event - 04-05-2014.mp3"


def _convention(
    *,
    scope_value: str = "alphagrp",
    convention_value: str | None = "DD-MM",
    supporting: int = 500,
    contradicting: int = 0,
    ambiguous: int = 120,
) -> FilenameConvention:
    """An UNPERSISTED convention row with a hand-set ``confidence``.

    ``confidence`` is a generated column in Postgres, so it is read-only there; on a detached ORM
    instance it is a plain attribute, which is exactly what makes the pure gate testable without a
    database. The DB-backed tests below use real rows where that distinction matters.
    """
    row = FilenameConvention(
        id=uuid.uuid4(),
        scope=CONVENTION_SCOPE,
        scope_value=scope_value,
        convention_kind=CONVENTION_KIND,
        convention_value=convention_value,
        supporting_count=supporting,
        contradicting_count=contradicting,
        ambiguous_count=ambiguous,
        computed_at=COMPUTED_AT,
    )
    total = supporting + contradicting
    row.confidence = supporting / total if total else 0.0
    return row


def _resolve(filename: str, convention: FilenameConvention | None, **overrides: Any) -> DateProvenance | None:
    kwargs: dict[str, Any] = {"min_supporting": 50, "min_purity": 0.99}
    kwargs.update(overrides)
    return resolve_date(filename, convention=convention, **kwargs)


# --------------------------------------------------------------------------------------------
# Precedence: a self-resolving date always wins
# --------------------------------------------------------------------------------------------


class TestSelfResolvingAlwaysWins:
    def test_a_self_resolving_date_is_read_from_the_filename(self) -> None:
        provenance = _resolve(SELF_RESOLVING_MONTH_FIRST, None)
        assert provenance is not None
        assert (provenance.date, provenance.source, provenance.date_order) == ("2014-04-25", SOURCE_FILENAME, "MM-DD")

    def test_a_self_resolving_date_is_not_overridden_by_a_contradicting_convention(self) -> None:
        """The convention was LEARNED from files like this one -- it can never outrank one."""
        provenance = _resolve(SELF_RESOLVING_MONTH_FIRST, _convention(convention_value="DD-MM"))
        assert provenance is not None
        assert provenance.date_order == "MM-DD"
        assert provenance.source == SOURCE_FILENAME
        assert provenance.scope_value is None, "a self-resolving date must carry no convention provenance"

    def test_the_other_self_resolving_direction_reads_the_same_way(self) -> None:
        provenance = _resolve(SELF_RESOLVING_DAY_FIRST, _convention(convention_value="MM-DD"))
        assert provenance is not None
        assert (provenance.date, provenance.date_order) == ("2014-04-25", "DD-MM")


# --------------------------------------------------------------------------------------------
# The two thresholds
# --------------------------------------------------------------------------------------------


class TestGates:
    def test_an_above_bar_convention_resolves_an_ambiguous_date(self) -> None:
        provenance = _resolve(AMBIGUOUS, _convention(convention_value="DD-MM", supporting=500, contradicting=0))
        assert provenance is not None
        assert (provenance.date, provenance.source) == ("2014-05-04", SOURCE_CONVENTION)
        assert provenance.raw == "04-05-2014"

    def test_the_month_first_convention_reads_the_same_token_the_other_way(self) -> None:
        provenance = _resolve(AMBIGUOUS, _convention(convention_value="MM-DD"))
        assert provenance is not None
        assert provenance.date == "2014-04-05"

    def test_too_little_evidence_leaves_the_date_unresolved(self) -> None:
        assert _resolve(AMBIGUOUS, _convention(supporting=49, contradicting=0)) is None

    def test_exactly_the_evidence_bar_is_enough(self) -> None:
        assert _resolve(AMBIGUOUS, _convention(supporting=50, contradicting=0)) is not None

    def test_impure_evidence_leaves_the_date_unresolved_however_large(self) -> None:
        """1,000 supporting files do not make a convention safe when 400 contradict it."""
        assert _resolve(AMBIGUOUS, _convention(supporting=1000, contradicting=400)) is None

    def test_both_bars_are_required_not_either(self) -> None:
        # Pure but tiny.
        assert _resolve(AMBIGUOUS, _convention(supporting=3, contradicting=0)) is None
        # Large but impure.
        assert _resolve(AMBIGUOUS, _convention(supporting=900, contradicting=100)) is None

    def test_the_bars_are_configurable_not_baked_in(self) -> None:
        below_default = _convention(supporting=10, contradicting=1)
        assert _resolve(AMBIGUOUS, below_default) is None
        assert _resolve(AMBIGUOUS, below_default, min_supporting=10, min_purity=0.9) is not None

    def test_no_convention_at_all_leaves_the_date_unresolved(self) -> None:
        assert _resolve(AMBIGUOUS, None) is None

    def test_a_tied_convention_row_resolves_nothing(self) -> None:
        """The learner writes a NULL ``convention_value`` for a tied group -- nothing to apply."""
        assert _resolve(AMBIGUOUS, _convention(convention_value=None, supporting=0, contradicting=200)) is None

    def test_an_unrecognized_convention_value_fails_closed_and_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            assert _resolve(AMBIGUOUS, _convention(convention_value="YYYY-DD-MM")) is None
        assert "not a recognized order" in caplog.text


class TestNonDates:
    @pytest.mark.parametrize(
        "filename",
        [
            "Artist - Event - sbd-alphagrp.mp3",  # no date
            "Artist - Event - 2014.04.25 - sbd-alphagrp.mp3",  # out-of-scope shape
            "Artist - Event - 00-13-2014 - sbd-alphagrp.mp3",  # no date under either reading
            "Artist - 04-05-2014 - rip 11-12-2015 - sbd-alphagrp.mp3",  # two conflicting tokens
        ],
    )
    def test_nothing_to_resolve_yields_no_provenance(self, filename: str) -> None:
        assert _resolve(filename, _convention()) is None


class TestProvenanceShape:
    def test_a_convention_derived_date_carries_the_full_evidence(self) -> None:
        convention = _convention(supporting=1373, contradicting=0, ambiguous=753, convention_value="MM-DD")
        provenance = _resolve(AMBIGUOUS, convention)
        assert provenance is not None

        payload = provenance.as_dict()
        assert payload["source"] == SOURCE_CONVENTION
        assert payload["scope"] == CONVENTION_SCOPE
        assert payload["scope_value"] == "alphagrp"
        assert payload["convention_kind"] == CONVENTION_KIND
        assert payload["convention_value"] == "MM-DD"
        assert payload["convention_id"] == str(convention.id)
        assert (payload["supporting_count"], payload["contradicting_count"], payload["ambiguous_count"]) == (1373, 0, 753)
        assert payload["confidence"] == pytest.approx(1.0)
        assert payload["computed_at"] == COMPUTED_AT.isoformat()

    def test_computed_at_records_the_evidence_as_it_stood(self) -> None:
        """A later refresh must not silently restate an old proposal's justification."""
        provenance = _resolve(AMBIGUOUS, _convention())
        assert provenance is not None
        assert provenance.computed_at == COMPUTED_AT.isoformat()

    def test_a_filename_derived_date_leaves_every_convention_field_null(self) -> None:
        provenance = _resolve(SELF_RESOLVING_MONTH_FIRST, None)
        assert provenance is not None
        payload = provenance.as_dict()
        convention_fields = [
            "scope",
            "scope_value",
            "convention_kind",
            "convention_value",
            "convention_id",
            "supporting_count",
            "contradicting_count",
            "ambiguous_count",
            "confidence",
            "computed_at",
        ]
        assert all(payload[key] is None for key in convention_fields)
        assert set(payload) == {"date", "raw", "date_order", "source", *convention_fields}


# --------------------------------------------------------------------------------------------
# Batched store lookup + context annotation (real Postgres)
# --------------------------------------------------------------------------------------------


async def _persist(session: AsyncSession, **kwargs: Any) -> FilenameConvention:
    row = FilenameConvention(
        scope=CONVENTION_SCOPE,
        convention_kind=CONVENTION_KIND,
        **kwargs,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class TestLoadReleaseGroupConventions:
    async def test_loads_only_the_asked_for_groups_of_this_scope_and_kind(self, session: AsyncSession) -> None:
        await _persist(session, scope_value="alphagrp", convention_value="DD-MM", supporting_count=100, contradicting_count=0)
        await _persist(session, scope_value="betagrp", convention_value="MM-DD", supporting_count=100, contradicting_count=0)
        other_kind = FilenameConvention(
            scope=CONVENTION_SCOPE,
            scope_value="alphagrp",
            convention_kind="separator_style",
            convention_value="hyphen",
            supporting_count=9,
            contradicting_count=0,
        )
        session.add(other_kind)
        await session.commit()

        loaded = await load_release_group_conventions(session, ["alphagrp", "missinggrp"])

        assert set(loaded) == {"alphagrp"}
        assert loaded["alphagrp"].convention_value == "DD-MM"

    async def test_an_empty_group_set_issues_no_query(self, session: AsyncSession) -> None:
        assert await load_release_group_conventions(session, []) == {}


class TestAnnotateDateConventions:
    async def test_flag_off_annotates_nothing_at_all(self, session: AsyncSession) -> None:
        """THE fail-closed invariant: with the flag off the contexts come back untouched."""
        await _persist(session, scope_value="alphagrp", convention_value="DD-MM", supporting_count=500, contradicting_count=0)
        contexts = [{"original_filename": AMBIGUOUS}, {"original_filename": SELF_RESOLVING_MONTH_FIRST}]
        before = [dict(context) for context in contexts]

        annotated = await annotate_date_conventions(session, contexts, enabled=False, min_supporting=1, min_purity=0.0)

        assert annotated == 0
        assert contexts == before

    async def test_flag_on_resolves_an_ambiguous_date_from_a_qualifying_group(self, session: AsyncSession) -> None:
        row = await _persist(
            session, scope_value="alphagrp", convention_value="DD-MM", supporting_count=500, contradicting_count=0, ambiguous_count=7
        )
        contexts = [{"original_filename": AMBIGUOUS}]

        annotated = await annotate_date_conventions(session, contexts, enabled=True, min_supporting=50, min_purity=0.99)

        assert annotated == 1
        provenance = contexts[0][CONTEXT_KEY]
        assert provenance["date"] == "2014-05-04"
        assert provenance["source"] == SOURCE_CONVENTION
        assert provenance["convention_id"] == str(row.id)

    async def test_flag_on_leaves_a_below_bar_group_unresolved(self, session: AsyncSession) -> None:
        await _persist(session, scope_value="alphagrp", convention_value="DD-MM", supporting_count=4, contradicting_count=0)
        contexts = [{"original_filename": AMBIGUOUS}]

        annotated = await annotate_date_conventions(session, contexts, enabled=True, min_supporting=50, min_purity=0.99)

        assert annotated == 0
        assert CONTEXT_KEY not in contexts[0]

    async def test_flag_on_resolves_a_self_resolving_date_with_no_convention_present(self, session: AsyncSession) -> None:
        contexts = [{"original_filename": SELF_RESOLVING_MONTH_FIRST}]

        annotated = await annotate_date_conventions(session, contexts, enabled=True, min_supporting=50, min_purity=0.99)

        assert annotated == 1
        assert contexts[0][CONTEXT_KEY]["source"] == SOURCE_FILENAME

    async def test_a_mixed_batch_annotates_exactly_the_resolvable_files(self, session: AsyncSession) -> None:
        await _persist(session, scope_value="alphagrp", convention_value="DD-MM", supporting_count=500, contradicting_count=0)
        contexts: list[dict[str, Any]] = [
            {"original_filename": SELF_RESOLVING_MONTH_FIRST},
            {"original_filename": AMBIGUOUS},
            {"original_filename": NO_GROUP},
            {"original_filename": "Artist - Event - sbd-betagrp.mp3"},
            {"original_filename": "Artist - Event - 04-05-2014 - sbd-betagrp.mp3"},
        ]

        annotated = await annotate_date_conventions(session, contexts, enabled=True, min_supporting=50, min_purity=0.99)

        assert annotated == 2
        assert contexts[0][CONTEXT_KEY]["source"] == SOURCE_FILENAME
        assert contexts[1][CONTEXT_KEY]["source"] == SOURCE_CONVENTION
        assert CONTEXT_KEY not in contexts[2], "no claimable release group -> nothing to fall back on"
        assert CONTEXT_KEY not in contexts[3], "no date -> nothing to resolve"
        assert CONTEXT_KEY not in contexts[4], "a group with no convention row -> unresolved"

    async def test_a_context_without_a_filename_is_survived(self, session: AsyncSession) -> None:
        contexts: list[dict[str, Any]] = [{}, {"original_filename": None}]

        assert await annotate_date_conventions(session, contexts, enabled=True, min_supporting=1, min_purity=0.0) == 0

    async def test_a_real_row_confidence_gates_on_the_db_derived_value(self, session: AsyncSession) -> None:
        """Purity is read off the generated column, never recomputed here -- so a real row proves it."""
        await _persist(session, scope_value="alphagrp", convention_value="DD-MM", supporting_count=90, contradicting_count=10)
        contexts = [{"original_filename": AMBIGUOUS}]

        assert await annotate_date_conventions(session, contexts, enabled=True, min_supporting=50, min_purity=0.99) == 0
        assert await annotate_date_conventions(session, contexts, enabled=True, min_supporting=50, min_purity=0.9) == 1
