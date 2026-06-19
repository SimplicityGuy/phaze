"""Tests for the SchedulingLedger model (Phase 45 Plan 01, Task 1).

The ledger records "this ``<function>:<natural_id>`` was scheduled" at the single
``before_enqueue`` chokepoint so recovery can replay exactly the previously-scheduled
work (and never the never-scheduled DISCOVERED backlog -- the 2026-06-18 incident).

These are pure-model tests: PK / column shape, metadata registration, and a field
round-trip. The reversible-migration tests live in
``tests/test_migrations/test_022.py``.
"""

import uuid

from phaze.models.base import Base
from phaze.models.scheduling_ledger import SchedulingLedger


class TestSchedulingLedgerSchema:
    """Table name / columns / PK / index shape."""

    def test_table_name(self) -> None:
        assert SchedulingLedger.__tablename__ == "scheduling_ledger"

    def test_table_in_metadata(self) -> None:
        assert "scheduling_ledger" in Base.metadata.tables

    def test_required_columns(self) -> None:
        columns = {c.name for c in SchedulingLedger.__table__.columns}
        required = {
            "key",
            "function",
            "routing",
            "payload",
            "enqueued_at",
            "created_at",
            "updated_at",
        }
        assert required.issubset(columns)

    def test_key_is_primary_key(self) -> None:
        pk_cols = [c.name for c in SchedulingLedger.__table__.primary_key.columns]
        assert pk_cols == ["key"]

    def test_no_foreign_keys(self) -> None:
        # The row must survive even if its target file/tracklist row is mid-flight;
        # the natural id lives inside payload. NO FK to files/tracklists.
        for col in SchedulingLedger.__table__.columns:
            assert not col.foreign_keys, f"column {col.name} must have no foreign key"

    def test_function_index_exists(self) -> None:
        # A plain index on ``function`` for per-stage diagnostics.
        indexed = {col.name for idx in SchedulingLedger.__table__.indexes for col in idx.columns}
        assert "function" in indexed


class TestSchedulingLedgerInstantiation:
    """A ledger row round-trips its fields in-memory."""

    def test_model_instantiation(self) -> None:
        fid = uuid.uuid4()
        row = SchedulingLedger(
            key=f"process_file:{fid}",
            function="process_file",
            routing="agent",
            payload={"file_id": str(fid), "agent_id": "nox"},
        )
        assert row.key == f"process_file:{fid}"
        assert row.function == "process_file"
        assert row.routing == "agent"
        assert row.payload == {"file_id": str(fid), "agent_id": "nox"}

    def test_payload_holds_arbitrary_json(self) -> None:
        row = SchedulingLedger(
            key="generate_proposals:abc",
            function="generate_proposals",
            routing="controller",
            payload={"file_ids": ["a", "b", "c"], "nested": {"k": 1}},
        )
        assert row.payload["file_ids"] == ["a", "b", "c"]
        assert row.payload["nested"]["k"] == 1
