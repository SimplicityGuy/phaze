"""Unit tests for phaze.schemas.agent_scan_batches (Phase 27 Plan 02 — D-10).

ScanBatchPatch is the request body for `PATCH /api/internal/agent/scan-batches/{batch_id}`.
ScanBatchPatchResponse is the row-echo response body.

D-10 invariants asserted here:
- `status` is restricted to `Literal["running", "completed", "failed"]` — the
  LIVE sentinel is the watcher's terminal state and MUST NOT be PATCH-able.
- `extra="forbid"` rejects unknown fields on the PATCH body.
- All four PATCH fields are optional; an empty PATCH validates (caller may
  send a subset, e.g. only `processed_files=N`).
- The response model is loose (no `extra="forbid"`) — server-built objects
  should be extensible without breaking the wire.
"""

from __future__ import annotations

import uuid

import pydantic
import pytest

from phaze.schemas.agent_scan_batches import ScanBatchPatch, ScanBatchPatchResponse


# -----------------------
# ScanBatchPatch
# -----------------------


def test_scan_batch_patch_accepts_running_status() -> None:
    p = ScanBatchPatch(status="running")
    assert p.status == "running"


def test_scan_batch_patch_rejects_live_status() -> None:
    """D-10 invariant: LIVE is a watcher-only terminal sentinel; never PATCH-able."""
    with pytest.raises(pydantic.ValidationError):
        ScanBatchPatch(status="live")  # type: ignore[arg-type]


def test_scan_batch_patch_rejects_garbage_status() -> None:
    with pytest.raises(pydantic.ValidationError):
        ScanBatchPatch(status="garbage")  # type: ignore[arg-type]


def test_scan_batch_patch_accepts_progress_counts() -> None:
    p = ScanBatchPatch(total_files=100, processed_files=50)
    assert p.total_files == 100
    assert p.processed_files == 50


def test_scan_batch_patch_no_ge_constraint_on_counts() -> None:
    """Schema shape matches sibling ExecutionLogPatch — no `ge=` on integer fields.

    D-10 does not specify integer-range constraints; controller may enforce.
    """
    p = ScanBatchPatch(total_files=-1)
    assert p.total_files == -1


def test_scan_batch_patch_rejects_unknown_field() -> None:
    """extra='forbid' rejects fields not in the four documented PATCH knobs."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ScanBatchPatch.model_validate({"unknown_field": "x"})

    assert any(e.get("type") == "extra_forbidden" for e in exc_info.value.errors())


def test_scan_batch_patch_empty_body_is_valid() -> None:
    """All four fields optional; agent can PATCH a subset (idempotent retry-friendly)."""
    p = ScanBatchPatch()
    assert p.model_dump(exclude_unset=True) == {}


def test_scan_batch_patch_response_full_row_echo() -> None:
    """D-Discretion §4: PATCH echoes the full updated batch row (no follow-up GET)."""
    bid = uuid.uuid4()
    r = ScanBatchPatchResponse(
        batch_id=bid,
        agent_id="agent-a",
        scan_path="/data/music",
        status="running",
        total_files=0,
        processed_files=0,
    )
    assert r.batch_id == bid
    assert r.error_message is None


def test_scan_batch_patch_status_json_schema_excludes_live() -> None:
    """JSON schema's `status` Literal alternative MUST NOT include 'live'."""
    schema = ScanBatchPatch.model_json_schema()
    status_entry = schema["properties"]["status"]
    # Pydantic v2 renders Literal[...] | None as anyOf with each literal value + null
    any_of = status_entry.get("anyOf")
    assert any_of is not None
    # Each Literal value appears as {"const": "<value>"} or {"enum": [...]} inside anyOf.
    literal_values: set[str] = set()
    for option in any_of:
        if not isinstance(option, dict):
            continue
        if "const" in option:
            literal_values.add(option["const"])
        if "enum" in option:
            literal_values.update(option["enum"])
    assert literal_values == {"running", "completed", "failed"}, f"Got {literal_values!r}"
