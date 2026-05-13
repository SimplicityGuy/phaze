"""Unit tests for phaze.schemas.agent_files (Phase 27 Plan 02 — D-09).

Phase 27 adds exactly one optional field — `batch_id: uuid.UUID | None = None` —
to `FileUpsertChunk`. The field is `extra="forbid"`-compatible (the constraint
applies to UNKNOWN fields; `batch_id` is now a KNOWN field). Phase 25 callers
that omit `batch_id` continue to validate; new callers (Phase 27 scan_directory
+ watcher) may attach a UUID to bind the chunk to a specific ScanBatch.

LIVE-sentinel resolution (when `batch_id` is omitted) happens at the router
layer — Plan 03 wires it. This schema-level plan only adds the field shape.
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_files import FileUpsertChunk, FileUpsertRecord


def _make_record() -> FileUpsertRecord:
    return FileUpsertRecord(
        sha256_hash="a" * 64,
        original_path="/music/a.mp3",
        original_filename="a.mp3",
        current_path="/music/a.mp3",
        file_type="mp3",
        file_size=1024,
    )


def test_file_upsert_chunk_batch_id_defaults_to_none() -> None:
    """Phase 25 backwards compat: omitting batch_id continues to validate."""
    chunk = FileUpsertChunk(files=[_make_record()])
    assert chunk.batch_id is None


def test_file_upsert_chunk_accepts_explicit_batch_id() -> None:
    """Phase 27 D-09: an explicit UUID binds the chunk to a specific ScanBatch."""
    bid = uuid.uuid4()
    chunk = FileUpsertChunk(files=[_make_record()], batch_id=bid)
    assert chunk.batch_id == bid


def test_file_upsert_chunk_rejects_non_uuid_batch_id() -> None:
    """Pydantic UUID coercion fails on arbitrary strings."""
    with pytest.raises(pydantic.ValidationError):
        FileUpsertChunk(files=[_make_record()], batch_id="not-a-uuid")  # type: ignore[arg-type]


def test_file_upsert_chunk_extra_forbid_still_enforced() -> None:
    """`extra="forbid"` still rejects UNKNOWN fields even when batch_id is set."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        FileUpsertChunk.model_validate(
            {
                "files": [
                    {
                        "sha256_hash": "a" * 64,
                        "original_path": "/x",
                        "original_filename": "x",
                        "current_path": "/x",
                        "file_type": "mp3",
                        "file_size": 1,
                    },
                ],
                "batch_id": None,
                "extra_field": "x",
            },
        )

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_file_upsert_chunk_json_schema_includes_batch_id_uuid_or_null() -> None:
    """JSON schema exposes `batch_id` with UUID format OR null (Optional[UUID])."""
    schema = FileUpsertChunk.model_json_schema()
    props = schema["properties"]
    assert "batch_id" in props
    entry = props["batch_id"]
    # Pydantic v2 renders Optional[UUID] as anyOf [{"format": "uuid", ...}, {"type": "null"}]
    any_of = entry.get("anyOf")
    assert any_of is not None, f"Expected anyOf in {entry!r}"
    formats = {opt.get("format") for opt in any_of if isinstance(opt, dict)}
    types = {opt.get("type") for opt in any_of if isinstance(opt, dict)}
    assert "uuid" in formats
    assert "null" in types
