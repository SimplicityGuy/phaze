"""Pydantic schemas for scan API endpoints."""

from datetime import datetime
import uuid

from pydantic import BaseModel


class ScanRequest(BaseModel):
    """Request body for triggering a file scan."""

    path: str | None = None


class ScanResponse(BaseModel):
    """Response returned after starting a scan."""

    batch_id: uuid.UUID
    message: str


class ScanStatusResponse(BaseModel):
    """Response for scan status queries."""

    batch_id: uuid.UUID
    status: str
    scan_path: str
    total_files: int
    processed_files: int
    error_message: str | None
    created_at: datetime
