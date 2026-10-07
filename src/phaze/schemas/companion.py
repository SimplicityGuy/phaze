"""Pydantic schemas for companion association and duplicate detection endpoints."""

import uuid

from pydantic import BaseModel


class AssociateResponse(BaseModel):
    """Response from companion association endpoint."""

    new_associations: int
    removed_associations: int = 0
    """Links removed because the linking chain, re-run on current features, no longer derives them."""
    kept_associations: int = 0
    """Links the re-derivation confirmed unchanged."""
    awaiting_features: int = 0
    """Unlinked companions not decided because they have no current content features yet."""
    message: str


class DuplicateFile(BaseModel):
    """A single file within a duplicate group."""

    id: uuid.UUID
    original_path: str
    file_size: int
    file_type: str


class DuplicateGroup(BaseModel):
    """A group of files sharing the same SHA256 hash."""

    sha256_hash: str
    count: int
    files: list[DuplicateFile]


class DuplicateGroupsResponse(BaseModel):
    """Paginated response of duplicate groups."""

    groups: list[DuplicateGroup]
    total_groups: int
    limit: int
    offset: int
