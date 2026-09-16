"""The ORM half of the set projection (phaze-x1qr3.1).

Its sibling is ``tests/integration/test_migrations/test_063_set_projection.py``, which asserts
the same shapes against real migrated DDL. The split is deliberate, and is the rule about verifying
with the artifact's real consumer in
``docs/design/0012-verification-fidelity-and-operator-attribution.md``: the migration file and the
model are two independent declarations of one schema, and a test exercising only one would pass
while the other drifted.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models import AnalysisWindow, FileRecord, SetProfile
from phaze.services.set_projection import MOOD_ORDER


def test_analysis_window_gains_the_two_projection_columns() -> None:
    """``AnalysisWindow`` carries energy / mood_scores, both NULLABLE.

    Nullable is the load-bearing half, not a detail: migration 063 adds these to a table already
    holding millions of rows, and the bead's premise is that no existing row is rewritten. A NOT
    NULL column here would silently contradict the migration it is supposed to mirror.

    ``camelot`` is deliberately absent from this list: migration 065 (phaze-6r3eh) dropped it as
    a stored column, so it is no longer in ``__table__.columns`` at all -- it is a read-time
    ``@property`` instead (see ``test_analysis_window_camelot_is_a_read_time_property_not_a_column``
    below and ``test_analysis_window_camelot_property_equals_camelot_code_for_every_canonical_key``
    in ``tests/shared/services/test_set_projection.py``).
    """
    columns = AnalysisWindow.__table__.columns
    assert {"energy", "mood_scores"} <= set(columns.keys())
    assert all(columns[name].nullable for name in ("energy", "mood_scores"))


def test_analysis_window_camelot_is_a_read_time_property_not_a_column() -> None:
    """Migration 065 (phaze-6r3eh) dropped the stored ``camelot`` column; the attribute survives
    as a plain Python property, not a mapped column, and the class still answers to ``.camelot``.
    """
    columns = AnalysisWindow.__table__.columns
    assert "camelot" not in columns.keys()  # noqa: SIM118 - `.keys()` documents this is the ColumnCollection, not a mapping literal
    assert isinstance(AnalysisWindow.__dict__["camelot"], property)


def test_analysis_window_docstring_names_the_mood_order_constant() -> None:
    """Acceptance: the model docstring names ``services/set_projection.MOOD_ORDER``.

    ``mood_scores`` is an opaque JSONB blob to anyone reading the model alone -- its key set and
    key ORDER are a contract carried in exactly one place, and the docstring is where a reader of
    the column finds it. Asserted rather than trusted because a docstring is the first thing a
    later refactor rewrites.
    """
    docstring = AnalysisWindow.__doc__ or ""
    assert "set_projection.MOOD_ORDER" in docstring


def test_set_profile_declares_the_projection_columns() -> None:
    """``SetProfile`` carries exactly the per-file projection the bead specifies."""
    columns = SetProfile.__table__.columns
    assert set(columns.keys()) == {
        "file_id",
        "mean_vector",
        "arc",
        "glyph",
        "camelot_modal",
        "harmonic_discipline",
        "peak_sec",
        "projection_version",
        # From TimestampMixin -- phaze-cz3m requires both, timezone-aware.
        "created_at",
        "updated_at",
    }
    # file_id IS the key: one profile per file, so a second row for the same file is impossible.
    assert columns["file_id"].primary_key
    # A profile row exists because the projection ran, so "no version" is not a state.
    assert not columns["projection_version"].nullable
    # Everything else is nullable: a fine-only file (661 today, phaze-hia9z) has no coarse windows
    # and therefore no vector, arc or glyph. The epic's honesty rule says that shows as a gap, not
    # as a zero vector that would read as a real measurement of a silent set.
    assert all(columns[name].nullable for name in ("mean_vector", "arc", "glyph", "camelot_modal", "harmonic_discipline", "peak_sec"))


def test_set_profile_foreign_key_cascades_at_the_database_level() -> None:
    """The FK declares ON DELETE CASCADE, not just an ORM-level cascade rule.

    ``services/scan_deletion.py`` deletes file sidecars with raw ``DELETE`` statements and omits
    exactly the ones whose FK cascades. An ORM-only cascade would leave this row behind for that
    path and make the batch permanently undeletable -- the stage_skip defect that module's comments
    record having already paid for once.
    """
    (foreign_key,) = SetProfile.__table__.columns["file_id"].foreign_keys
    assert foreign_key.column.table.name == "files"
    assert foreign_key.ondelete == "CASCADE"


def test_file_record_relationship_lets_the_database_do_the_delete() -> None:
    """``FileRecord.set_profile`` cascades and defers to the DB rather than loading the child.

    ``passive_deletes`` is the half that is easy to omit and impossible to notice: with
    ``lazy="noload"`` and no ``passive_deletes``, SQLAlchemy tries to load the child in order to
    NULL its FK, gets nothing back because the relationship refuses to load, and the unit of work
    silently cascades over an empty collection.
    """
    relationship = FileRecord.__mapper__.relationships["set_profile"]
    assert relationship.mapper.class_ is SetProfile
    assert relationship.uselist is False
    assert relationship.passive_deletes is True
    assert "delete-orphan" in relationship.cascade


@pytest.mark.asyncio
async def test_deleting_a_file_removes_its_set_profile(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """Acceptance: the cascade removes the profile with the file.

    Exercised end to end against a real Postgres session rather than asserted off the mapping, so
    both halves have to be right at once: the ORM must not block the delete, and the FK's ON DELETE
    CASCADE must actually remove the row.
    """
    record = await make_file()
    session.add(SetProfile(file_id=record.id, camelot_modal="8A", peak_sec=42.0, projection_version=1))
    await session.commit()
    assert (await session.execute(select(SetProfile).where(SetProfile.file_id == record.id))).scalar_one_or_none() is not None

    await session.execute(FileRecord.__table__.delete().where(FileRecord.id == record.id))
    await session.commit()

    assert (await session.execute(select(SetProfile).where(SetProfile.file_id == record.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_orm_session_delete_also_removes_the_profile(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """The ORM delete path works too, not just the raw ``DELETE`` the deletion cascade issues.

    Separate from the test above on purpose. That one issues a Core ``DELETE``, which exercises
    only the FK's ON DELETE CASCADE; this one goes through ``session.delete``, where the unit of
    work gets a say. The mapping test asserts ``passive_deletes`` is set, and this is what
    OBSERVES the behaviour that setting buys -- a configuration assertion vouching for an
    unexercised code path is the trap CLAUDE.md names, and the two are only one test apart.
    """
    record = await make_file()
    session.add(SetProfile(file_id=record.id, projection_version=1))
    await session.commit()

    await session.delete(record)
    await session.commit()

    assert (await session.execute(select(SetProfile).where(SetProfile.file_id == record.id))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_a_window_round_trips_the_projection_columns(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    """A window stores and reads back energy, a full MOOD_ORDER-keyed dict, and ``musical_key``
    whose read-time ``camelot`` property (migration 065, phaze-6r3eh -- no longer a stored column)
    reflects it after the round trip through the database, same as before the migration.
    """
    record = await make_file()
    scores = {name: round(0.05 * index, 3) for index, name in enumerate(MOOD_ORDER)}
    session.add(
        AnalysisWindow(
            id=uuid.uuid4(),
            file_id=record.id,
            tier="coarse",
            window_index=0,
            start_sec=0.0,
            end_sec=180.0,
            energy=0.61,
            musical_key="E major",
            mood_scores=scores,
        )
    )
    await session.commit()

    stored = (await session.execute(select(AnalysisWindow).where(AnalysisWindow.file_id == record.id))).scalar_one()
    assert stored.energy == pytest.approx(0.61)
    assert stored.camelot == "12B"
    assert stored.mood_scores == scores
