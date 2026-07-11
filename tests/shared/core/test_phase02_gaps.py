"""Phase 02 gap-filling tests.

Covers behaviors not exercised by the existing 35 tests:
- ScanBatch model: tablename and field definitions
- ScanStatus enum: all values present

The ``run_scan`` orchestration tests were removed in Phase 89 (LEGACY-01) alongside
``services/ingestion.py``; the ScanBatch/ScanStatus model coverage below survives.
"""

from __future__ import annotations

from phaze.models.scan_batch import ScanBatch, ScanStatus


# ---------------------------------------------------------------------------
# ScanStatus enum
# ---------------------------------------------------------------------------


def test_scan_status_has_four_values() -> None:
    """ScanStatus enum contains exactly RUNNING, COMPLETED, FAILED, LIVE."""
    members = list(ScanStatus)
    assert len(members) == 4
    assert ScanStatus.RUNNING == "running"
    assert ScanStatus.COMPLETED == "completed"
    assert ScanStatus.FAILED == "failed"
    assert ScanStatus.LIVE == "live"


# ---------------------------------------------------------------------------
# ScanBatch model fields
# ---------------------------------------------------------------------------


def test_scan_batch_tablename() -> None:
    """ScanBatch maps to the 'scan_batches' table."""
    assert ScanBatch.__tablename__ == "scan_batches"


def test_scan_batch_has_required_columns() -> None:
    """ScanBatch model exposes the expected column names."""
    col_names = {col.name for col in ScanBatch.__table__.columns}
    required = {"id", "scan_path", "status", "total_files", "processed_files", "error_message", "created_at", "updated_at"}
    assert required <= col_names


def test_scan_batch_status_default() -> None:
    """ScanBatch.status column has a default of ScanStatus.RUNNING."""
    status_col = ScanBatch.__table__.columns["status"]
    assert status_col.default.arg == ScanStatus.RUNNING


def test_scan_batch_error_message_nullable() -> None:
    """ScanBatch.error_message column is nullable."""
    col = ScanBatch.__table__.columns["error_message"]
    assert col.nullable is True
