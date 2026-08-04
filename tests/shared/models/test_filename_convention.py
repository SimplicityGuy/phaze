"""Tests for the FilenameConvention model (phaze-5fta.2).

Pure-schema tests run without a DB; the derivation / uniqueness / CHECK-constraint tests use
the ``session`` fixture (real Postgres via ``Base.metadata.create_all``). The reversible
migration is covered in ``tests/integration/test_migrations/test_baseline_schema.py``
(``test_alembic_version_is_head``, ``test_upgrade_downgrade_roundtrip``,
``test_autogenerate_drift_is_frozen`` -- proving the migration and the ORM model agree exactly).
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError

from phaze.models.base import Base
from phaze.models.filename_convention import FilenameConvention


def _make_convention(**overrides: object) -> FilenameConvention:
    defaults: dict[str, object] = {
        "scope": "release_group",
        "scope_value": f"group-{uuid.uuid4().hex[:8]}",
        "convention_kind": "date_order",
        "convention_value": "MM-DD",
        "supporting_count": 0,
        "contradicting_count": 0,
        "ambiguous_count": 0,
    }
    defaults.update(overrides)
    return FilenameConvention(**defaults)  # type: ignore[arg-type]


class TestFilenameConventionSchema:
    """Table name / columns / constraint shape -- no DB required."""

    def test_table_name(self) -> None:
        assert FilenameConvention.__tablename__ == "filename_convention"

    def test_table_in_metadata(self) -> None:
        assert "filename_convention" in Base.metadata.tables

    def test_required_columns(self) -> None:
        columns = {c.name for c in FilenameConvention.__table__.columns}
        required = {
            "id",
            "scope",
            "scope_value",
            "convention_kind",
            "convention_value",
            "supporting_count",
            "contradicting_count",
            "ambiguous_count",
            "confidence",
            "computed_at",
            "created_at",
            "updated_at",
        }
        assert required.issubset(columns)

    def test_no_date_specific_columns(self) -> None:
        """The whole point of the generalized store: nothing here names a date (phaze-5fta.2).

        Excludes the generic `TimestampMixin` / `computed_at` timekeeping columns -- those track
        WHEN a row was written, not a learned filename DATE-ORDER convention, and `updated_at`
        would otherwise false-positive on the substring check below.
        """
        generic_timestamps = {"created_at", "updated_at", "computed_at"}
        columns = {c.name for c in FilenameConvention.__table__.columns} - generic_timestamps
        date_shaped = {c for c in columns if "date" in c.lower() or "dd_mm" in c.lower() or "mm_dd" in c.lower()}
        assert date_shaped == set()

    def test_id_is_primary_key(self) -> None:
        pk_cols = [c.name for c in FilenameConvention.__table__.primary_key.columns]
        assert pk_cols == ["id"]

    def test_unique_constraint_on_scope_triple(self) -> None:
        constraint_columns = {
            frozenset(c.name for c in uc.columns) for uc in FilenameConvention.__table__.constraints if uc.name and "uq_" in uc.name
        }
        assert frozenset({"scope", "scope_value", "convention_kind"}) in constraint_columns

    def test_confidence_is_a_generated_column(self) -> None:
        """The DB-level derivation guarantee: `confidence` MUST be a Postgres GENERATED ALWAYS AS
        (...) STORED column, not a plain writable one -- this is what makes hand-setting it
        impossible, not merely convention."""
        col = FilenameConvention.__table__.columns["confidence"]
        assert col.computed is not None
        assert col.computed.persisted is True

    def test_supporting_contradicting_ambiguous_default_to_zero(self) -> None:
        columns = FilenameConvention.__table__.columns
        for name in ("supporting_count", "contradicting_count", "ambiguous_count"):
            assert columns[name].server_default.arg == "0", name

    def test_scope_and_convention_kind_carry_no_enum_check(self) -> None:
        """scope/convention_kind must accommodate future values with NO migration (phaze-5fta.2) --
        so neither may be pinned by a CHECK ... IN (...) constraint."""
        check_sql = " ".join(str(c.sqltext) for c in FilenameConvention.__table__.constraints if hasattr(c, "sqltext") and c.sqltext is not None)
        assert "scope" not in check_sql.lower() or "IN (" not in check_sql
        assert "convention_kind" not in check_sql.lower() or "IN (" not in check_sql


class TestFilenameConventionPersistence:
    """Derivation / uniqueness / CHECK-constraint behavior against a real Postgres session."""

    async def test_round_trip(self, session) -> None:  # type: ignore[no-untyped-def]
        row = _make_convention(scope_value="talion", convention_value="MM-DD", supporting_count=1373, contradicting_count=0, ambiguous_count=753)
        session.add(row)
        await session.commit()
        await session.refresh(row)

        assert row.id is not None
        assert row.scope == "release_group"
        assert row.scope_value == "talion"
        assert row.convention_kind == "date_order"
        assert row.convention_value == "MM-DD"
        assert row.supporting_count == 1373
        assert row.contradicting_count == 0
        assert row.ambiguous_count == 753
        assert row.computed_at is not None
        assert row.created_at is not None

    async def test_confidence_derives_from_unanimous_evidence(self, session) -> None:  # type: ignore[no-untyped-def]
        """talion: 1,373 supporting / 0 contradicting -> confidence 1.0 (epic finding)."""
        row = _make_convention(scope_value="talion", supporting_count=1373, contradicting_count=0)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.confidence == pytest.approx(1.0)

    async def test_confidence_derives_from_mixed_evidence(self, session) -> None:  # type: ignore[no-untyped-def]
        row = _make_convention(scope_value="mixed-group", supporting_count=3, contradicting_count=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.confidence == pytest.approx(0.75)

    async def test_confidence_is_zero_with_no_supporting_or_contradicting_evidence(self, session) -> None:  # type: ignore[no-untyped-def]
        """No supporting/contradicting evidence yet (only ambiguous population tallied so far):
        confidence derives to 0.0, never NULL and never an application-chosen default."""
        row = _make_convention(scope_value="untallied-group", convention_value=None, supporting_count=0, contradicting_count=0, ambiguous_count=42)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.confidence == pytest.approx(0.0)

    async def test_ambiguous_count_does_not_affect_confidence(self, session) -> None:  # type: ignore[no-untyped-def]
        """ambiguous_count sizes the payoff of applying the convention -- it is NOT evidence for or
        against it (epic PRECEDENCE RULES), so two rows differing only in ambiguous_count must
        derive the identical confidence."""
        low = _make_convention(scope_value="low-ambiguous", supporting_count=10, contradicting_count=2, ambiguous_count=1)
        high = _make_convention(scope_value="high-ambiguous", supporting_count=10, contradicting_count=2, ambiguous_count=9000)
        session.add_all([low, high])
        await session.commit()
        await session.refresh(low)
        await session.refresh(high)
        assert low.confidence == pytest.approx(high.confidence)

    async def test_explicit_confidence_write_is_rejected_by_postgres(self, session) -> None:  # type: ignore[no-untyped-def]
        """The bead's central guarantee: a caller CANNOT write an arbitrary confidence that
        disagrees with the counts -- Postgres itself refuses any INSERT/UPDATE naming the
        generated column, regardless of what the ORM instance's attribute is set to."""
        row = _make_convention(scope_value="hand-set-attempt", supporting_count=232, contradicting_count=0)
        row.confidence = 0.01  # deliberately disagrees with the derived value (1.0)
        session.add(row)
        with pytest.raises(ProgrammingError, match=r"[Gg]enerated column"):
            await session.commit()

    async def test_unique_scope_triple_violation(self, session) -> None:  # type: ignore[no-untyped-def]
        session.add(_make_convention(scope_value="dupe-group", convention_kind="date_order"))
        await session.commit()

        session.add(_make_convention(scope_value="dupe-group", convention_kind="date_order"))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_same_scope_value_different_convention_kind_is_allowed(self, session) -> None:  # type: ignore[no-untyped-def]
        """The unique triple is (scope, scope_value, convention_kind) -- a DIFFERENT kind for the
        same scope_value is a distinct row, proving new convention kinds slot in with no schema
        change (phaze-5fta.2)."""
        session.add(_make_convention(scope_value="multi-kind-group", convention_kind="date_order"))
        session.add(_make_convention(scope_value="multi-kind-group", convention_kind="separator_style", convention_value="_"))
        await session.commit()

    async def test_negative_supporting_count_rejected(self, session) -> None:  # type: ignore[no-untyped-def]
        session.add(_make_convention(scope_value="negative-count", supporting_count=-1))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_negative_contradicting_count_rejected(self, session) -> None:  # type: ignore[no-untyped-def]
        session.add(_make_convention(scope_value="negative-count-2", contradicting_count=-1))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_negative_ambiguous_count_rejected(self, session) -> None:  # type: ignore[no-untyped-def]
        session.add(_make_convention(scope_value="negative-count-3", ambiguous_count=-1))
        with pytest.raises(IntegrityError):
            await session.commit()

    async def test_convention_value_nullable(self, session) -> None:  # type: ignore[no-untyped-def]
        row = _make_convention(scope_value="no-winner-yet", convention_value=None, supporting_count=1, contradicting_count=1)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.convention_value is None
        assert row.confidence == pytest.approx(0.5)
