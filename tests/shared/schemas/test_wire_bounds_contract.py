"""Mechanical enforcement of the wire-bounds contract (phaze-btlu).

``src/phaze/schemas/wire_bounds.py`` states the contract in prose. This module makes it FAIL THE
SUITE, so the eighth instance of the defect class cannot land the way the first seven did.

Two tiers, because the two entry shapes admit different levels of automation:

TIER 1 -- BODY FIELDS, AUTO-DERIVED (:data:`SCHEMA_BINDINGS`).
    A wire schema declares its destination MODEL once. Every ``str``/``int`` field whose name
    matches a mapped column is then checked against the LIVE SQLAlchemy column: ``String(N)``
    demands ``max_length == N`` (rule 1), ``Text`` demands no cap (rule 2), ``Integer`` demands a
    bound inside int4 (rule 3). Nothing is copied by hand, so a later migration that widens a
    column and forgets the schema is caught. A field of a bound schema that matches NO column must
    be named in :data:`UNMAPPED_BODY_FIELDS` with a reason -- so ADDING a field to a bound schema
    fails until someone classifies it.

TIER 2 -- PATH / QUERY / FORM PARAMS, REGISTERED (:data:`PARAM_CLASSIFICATIONS`).
    These have no declarative link to a column, so each governed param is classified by hand. The
    gate is that EVERY governed param must be classified: a new unclassified route param fails.

Both tiers share :data:`KNOWN_GAPS` -- the seven sibling beads of this defect class, recorded as
strict-xfail. A known gap must STILL BE VIOLATING; when a sibling lands its fix, the entry stops
matching and this test fails, telling that developer to delete the entry. The registry is therefore
a live checklist that empties itself rather than a suppression list that rots.
"""

from __future__ import annotations

from datetime import date, datetime
import typing
import uuid

from annotated_types import Ge, Le, MaxLen, MinLen
from pydantic import BaseModel
import pytest
from sqlalchemy import BigInteger, Integer, SmallInteger, String, Text
from sqlalchemy.inspection import inspect as sa_inspect

from phaze.main import create_app
from phaze.models import (
    Agent,
    AnalysisResult,
    AnalysisWindow,
    ExecutionLog,
    FileMetadata,
    FileRecord,
    FingerprintResult,
    RenameProposal,
    ScanBatch,
    Tracklist,
    TracklistTrack,
)
from phaze.schemas.agent_analysis import AnalysisWindowPayload, AnalysisWritePayload
from phaze.schemas.agent_execution import ExecutionLogCreate, ExecutionLogPatch
from phaze.schemas.agent_files import FileUpsertRecord
from phaze.schemas.agent_fingerprint import FingerprintWriteRequest
from phaze.schemas.agent_heartbeat import HeartbeatRequest
from phaze.schemas.agent_metadata import MetadataWriteRequest
from phaze.schemas.agent_proposals import ProposalStatePatch
from phaze.schemas.agent_scan_batches import ScanBatchPatch
from phaze.schemas.agent_tracklists import TracklistCreatePayload, TracklistTrackPayload
from phaze.schemas.wire_bounds import INT16_MAX, INT16_MIN, INT32_MAX, INT32_MIN, INT64_MAX, INT64_MIN
from tests._route_introspection import iter_effective_routes


# --------------------------------------------------------------------------------------------
# TIER 1 registry: wire schema -> the model whose columns it writes.
# --------------------------------------------------------------------------------------------
SCHEMA_BINDINGS: dict[type[BaseModel], type] = {
    FileUpsertRecord: FileRecord,
    MetadataWriteRequest: FileMetadata,
    FingerprintWriteRequest: FingerprintResult,
    ExecutionLogCreate: ExecutionLog,
    ExecutionLogPatch: ExecutionLog,
    HeartbeatRequest: Agent,
    AnalysisWritePayload: AnalysisResult,
    AnalysisWindowPayload: AnalysisWindow,
    TracklistCreatePayload: Tracklist,
    TracklistTrackPayload: TracklistTrack,
    ProposalStatePatch: RenameProposal,
    ScanBatchPatch: ScanBatch,
}

# Fields of a bound schema that intentionally match no column of that schema's model.
# Adding a field to a bound schema fails the suite until it appears here or matches a column.
UNMAPPED_BODY_FIELDS: dict[type[BaseModel], dict[str, str]] = {
    HeartbeatRequest: {
        # The whole heartbeat lands in Agent.last_status JSONB (agent_heartbeat.py), never in a
        # scalar column -- JSONB has no width to match, so rule 1/3 do not apply.
        "agent_version": "-> Agent.last_status JSONB, no scalar column",
        "worker_pid": "-> Agent.last_status JSONB, no scalar column",
        "queue_depth": "-> Agent.last_status JSONB, no scalar column",
        "lane": "-> Agent.last_status['lanes'] JSONB key, no scalar column",
    },
    ProposalStatePatch: {
        # This schema patches a proposal but writes these two ACROSS models -- both to Text columns,
        # so rule 2 applies and neither needs a cap.
        "current_path": "-> FileRecord.current_path Text (agent_proposals.py:116), unbounded (rule 2)",
        "error_message": "-> RenameProposal.reason Text (agent_proposals.py:105), unbounded (rule 2)",
    },
}


# --------------------------------------------------------------------------------------------
# KNOWN GAPS -- the sibling beads of this defect class. Strict xfail: each MUST still be violating.
# When your bead lands, DELETE your entry; leaving it fails the suite.
# --------------------------------------------------------------------------------------------
KNOWN_GAPS: dict[tuple[str, str], str] = {}

# Gaps this check FOUND that have no bead yet. Same defect class, same strict-xfail semantics; kept
# separate so the set awaiting triage is obvious rather than buried among the filed beads. Move an
# entry into KNOWN_GAPS once it is filed, and delete it once it is fixed.
UNFILED_GAPS: dict[tuple[str, str], str] = {
    ("AnalysisWritePayload", "musical_key"): "rule 1: no max_length vs analysis_results.musical_key String(10)",
    ("AnalysisWindowPayload", "musical_key"): "rule 1: no max_length vs analysis_windows.musical_key String(10)",
    ("AnalysisWindowPayload", "mood"): "rule 1: no max_length vs analysis_windows.mood String(50)",
    ("AnalysisWindowPayload", "style"): "rule 1: no max_length vs analysis_windows.style String(50)",
    ("FileUpsertRecord", "file_size"): "rule 3: ge=0 only, no upper bound vs files.file_size BigInteger (int8)",
    ("ScanBatchPatch", "total_files"): "rule 3: unbounded vs scan_batches.total_files Integer (int4)",
    ("ScanBatchPatch", "processed_files"): "rule 3: unbounded vs scan_batches.processed_files Integer (int4)",
}

ALL_GAPS: dict[tuple[str, str], str] = {**KNOWN_GAPS, **UNFILED_GAPS}


# --------------------------------------------------------------------------------------------
# TIER 2 registry: every governed path/query/form param, classified.
# --------------------------------------------------------------------------------------------
_PAGING = "paging param -- bounded by the pagination contract (wire_bounds rule 8)"
_WHITELIST = "validated against an in-route whitelist/enum before any column use"
_TEXT = "lands in a Text column -- unbounded, no cap needed (rule 2)"
_NOT_STORED = "never reaches a column; consumed as a control value in-route"

PARAM_CLASSIFICATIONS: dict[tuple[str, str], str] = {
    ("/proposals/", "status"): _WHITELIST,
    ("/proposals/", "q"): _TEXT,
    ("/proposals/", "sort"): _WHITELIST,
    ("/proposals/", "order"): _WHITELIST,
    ("/proposals/{proposal_id}/edit", "proposed"): _TEXT,
    ("/proposals/{proposal_id}/edit", "facet"): _WHITELIST,
    ("/proposals/bulk", "action"): _WHITELIST,
    ("/execution/progress/{batch_id}", "batch_id"): _NOT_STORED,
    ("/audit/", "status"): _WHITELIST,
    ("/duplicates/{group_hash}/compare", "group_hash"): _NOT_STORED,
    ("/duplicates/{group_hash}/resolve", "group_hash"): _NOT_STORED,
    ("/duplicates/{group_hash}/undo", "group_hash"): _NOT_STORED,
    ("/duplicates/{group_hash}/undo", "file_states"): _NOT_STORED,
    ("/duplicates/undo-all", "file_states"): _NOT_STORED,
    ("/tracklists/", "filter"): _WHITELIST,
    ("/tracklists/scan/status", "job_ids"): _NOT_STORED,
    ("/tracklists/scan/status", "agent_id"): "bounded: max_length=128 + pattern",
    ("/tracklists/link-result", "external_id"): "operator-facing mirror of the agent path; String(50) -- see phaze-btlu",
    ("/tracklists/link-result", "url"): _TEXT,
    ("/tracklists/tracks/{track_id}/edit/{field}", "field"): _WHITELIST,
    ("/tracklists/{tracklist_id}/reject-low", "threshold"): _NOT_STORED,
    ("/pipeline/stats", "lane"): _WHITELIST,
    ("/pipeline/lanes/{backend_id}", "backend_id"): _WHITELIST,
    ("/pipeline/files", "stage"): _WHITELIST,
    ("/pipeline/files", "bucket"): _WHITELIST,
    ("/pipeline/analyze-files", "status"): _WHITELIST,
    ("/pipeline/pending-files", "stage"): _WHITELIST,
    ("/pipeline/files/{file_id}/skip/{stage}", "stage"): _WHITELIST,
    ("/pipeline/files/{file_id}/skip/{stage}", "reason"): _TEXT,
    ("/pipeline/files/{file_id}/trace/{stage}", "stage"): _WHITELIST,
    ("/s/{stage}", "stage"): _WHITELIST,
    ("/pipeline/stages/{stage}/priority", "stage"): _WHITELIST,
    ("/pipeline/stages/{stage}/priority", "delta"): _NOT_STORED,
    ("/pipeline/stages/{stage}/pause", "stage"): _WHITELIST,
    ("/pipeline/stages/{stage}/resume", "stage"): _WHITELIST,
    ("/search/", "q"): _TEXT,
    ("/search/", "artist"): _TEXT,
    ("/search/", "genre"): _TEXT,
    ("/tags/{file_id}/edit/{field}", "field"): _WHITELIST,
    # Bulk-action id lists: FastAPI reports the ELEMENT type for a ``list[str] = Form(...)``, so they
    # surface here as ``str``. Each element is parsed to a UUID / compared to a known hash in-route.
    ("/proposals/bulk", "proposal_ids"): "list[str] Form; each element parsed to UUID in-route",
    ("/duplicates/resolve-all", "group_hashes"): "list[str] Form; each element matched against known group hashes",
    ("/tracklists/scan", "file_ids"): "list[str] Form; each element parsed to UUID in-route",
    # Trigger-scan form: validated server-side against the selected agent's ``scan_roots`` (D-06 /
    # WR-05) before any use, and never stored raw.
    ("/pipeline/scans", "agent_id"): "validated against the known agent set (D-06) before use",
    ("/pipeline/scans", "scan_root"): "must be literal member of agent.scan_roots (WR-05)",
    ("/pipeline/scans", "subpath"): "NFC-normalized + prefix-validated against agent.scan_roots (D-06)",
    ("/admin/agents", "agent"): "selector resolved against the loaded agent list, not stored",
    ("/admin/agents/_table", "agent"): "selector resolved against the loaded agent list, not stored",
    ("/admin/agents/{agent_id}/_activity", "agent_id"): "lookup key only; a miss renders the empty state",
}


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def _unwrap(annotation: object) -> object:
    """Strip ``| None`` so ``str | None`` is governed exactly like ``str``."""
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 else annotation


def _is_governed(annotation: object) -> bool:
    """True for the scalar wire types this contract bounds (``str``/``int``, not ``bool``)."""
    inner = _unwrap(annotation)
    if inner in (uuid.UUID, bool, date, datetime):
        return False
    if typing.get_origin(inner) is not None:  # Literal[...], list[...], dict[...] -- self-bounding
        return False
    return inner in (str, int)


def _constraint(metadata: list[object], kind: type) -> object | None:
    for m in metadata:
        if isinstance(m, kind):
            return m
    return None


def _bounds(metadata: list[object]) -> tuple[int | None, int | None, int | None, int | None]:
    """Return ``(max_length, min_length, ge, le)`` from a field's constraint metadata."""
    max_len = _constraint(metadata, MaxLen)
    min_len = _constraint(metadata, MinLen)
    ge = _constraint(metadata, Ge)
    le = _constraint(metadata, Le)
    return (
        max_len.max_length if max_len else None,  # type: ignore[union-attr]
        min_len.min_length if min_len else None,  # type: ignore[union-attr]
        ge.ge if ge else None,  # type: ignore[union-attr]
        le.le if le else None,  # type: ignore[union-attr]
    )


def _int_range(col_type: Integer) -> tuple[int, int, str]:
    """Return ``(min, max, label)`` for a concrete SQLAlchemy integer column type."""
    if isinstance(col_type, BigInteger):
        return INT64_MIN, INT64_MAX, "int8"
    if isinstance(col_type, SmallInteger):
        return INT16_MIN, INT16_MAX, "int2"
    return INT32_MIN, INT32_MAX, "int4"


def _violation(annotation: object, metadata: list[object], column: object) -> str | None:
    """Return a contract violation message for a field against its column, or ``None`` if compliant."""
    inner = _unwrap(annotation)
    max_len, _min_len, ge, le = _bounds(metadata)
    col_type = column.type  # type: ignore[attr-defined]

    if inner is str:
        if isinstance(col_type, String) and not isinstance(col_type, Text) and col_type.length:
            if max_len != col_type.length:
                return f"rule 1: max_length={max_len} but column is String({col_type.length}); they must be equal"
        elif isinstance(col_type, Text) and max_len is not None:
            return f"rule 2: max_length={max_len} on a Text column; Text is unbounded, drop the cap"
    elif inner is int and isinstance(col_type, Integer):
        # BigInteger/SmallInteger subclass Integer, so widen/narrow by the CONCRETE type -- an int8
        # column must not be judged against the int4 bound.
        low, high, width = _int_range(col_type)
        if le is None:
            return f"rule 3: no upper bound (le=) vs an {width} column; use a domain bound or le={high}"
        if le > high:
            return f"rule 3: le={le} exceeds {width} max {high}"
        if ge is not None and ge < low:
            return f"rule 3: ge={ge} below {width} min {low}"
    return None


def _body_cases() -> list[tuple[type[BaseModel], str, object, list[object], object | None]]:
    cases = []
    for schema, model in SCHEMA_BINDINGS.items():
        columns = {c.key: c for c in sa_inspect(model).columns}
        for name, field in schema.model_fields.items():
            if not _is_governed(field.annotation):
                continue
            cases.append((schema, name, field.annotation, list(field.metadata), columns.get(name)))
    return cases


def _param_cases() -> list[tuple[str, str, object, list[object]]]:
    app = create_app()
    cases = []
    for route in iter_effective_routes(app):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        params = list(dependant.path_params) + list(dependant.query_params) + list(dependant.body_params)
        for param in params:
            info = param.field_info
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                continue  # a body model -- tier 1 owns it
            if not _is_governed(annotation):
                continue
            cases.append((route.path, param.name, annotation, list(getattr(info, "metadata", []))))
    return cases


# --------------------------------------------------------------------------------------------
# TIER 1
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("schema", "name", "annotation", "metadata", "column"),
    _body_cases(),
    ids=lambda v: v.__name__ if isinstance(v, type) else (v if isinstance(v, str) else ""),
)
def test_body_field_is_bounded_to_its_column(
    schema: type[BaseModel],
    name: str,
    annotation: object,
    metadata: list[object],
    column: object | None,
) -> None:
    """Every governed body field either matches its column's bound or is a classified exception."""
    key = (schema.__name__, name)

    if column is None:
        reason = UNMAPPED_BODY_FIELDS.get(schema, {}).get(name)
        assert reason, (
            f"{schema.__name__}.{name} maps to no column of {SCHEMA_BINDINGS[schema].__name__} and is unclassified. "
            f"Add it to UNMAPPED_BODY_FIELDS with a reason, or bind it to the column it writes. "
            f"See src/phaze/schemas/wire_bounds.py."
        )
        return

    violation = _violation(annotation, metadata, column)

    if key in ALL_GAPS:
        assert violation is not None, (
            f"{schema.__name__}.{name} is listed as a known gap ({ALL_GAPS[key]}) but now COMPLIES. "
            f"The fix has landed -- delete its KNOWN_GAPS/UNFILED_GAPS entry."
        )
        pytest.xfail(ALL_GAPS[key])

    assert violation is None, f"{schema.__name__}.{name} violates the wire-bounds contract -- {violation}"


# --------------------------------------------------------------------------------------------
# TIER 2
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("path", "name", "annotation", "metadata"), _param_cases(), ids=lambda v: v if isinstance(v, str) else "")
def test_path_query_form_param_is_classified(path: str, name: str, annotation: object, metadata: list[object]) -> None:
    """Every governed path/query/form param is bounded, paging-owned, or explicitly classified."""
    key = (path, name)

    if key in ALL_GAPS:
        max_len, _min_len, _ge, le = _bounds(metadata)
        still_open = max_len is None and le is None
        assert still_open, (
            f"{path} :: {name} is listed as a known gap ({ALL_GAPS[key]}) but now carries a bound. "
            f"The fix has landed -- delete its KNOWN_GAPS/UNFILED_GAPS entry."
        )
        pytest.xfail(ALL_GAPS[key])

    if name in ("page", "page_size"):
        _max_len, _min_len, ge, _le = _bounds(metadata)
        assert ge is not None, f"{path} :: {name} is a paging param with no ge= guard (wire_bounds rule 8)"
        return

    max_len, _min_len, _ge, le = _bounds(metadata)
    if max_len is not None or le is not None:
        return  # carries an explicit bound

    assert key in PARAM_CLASSIFICATIONS, (
        f"{path} :: {name} crosses the HTTP boundary unbounded and unclassified. "
        f"Give it a bound matching the column it reaches (wire_bounds rules 1/3/5), or classify it in "
        f"PARAM_CLASSIFICATIONS with the reason it needs none. See src/phaze/schemas/wire_bounds.py."
    )


def test_known_gaps_reference_a_bead() -> None:
    """Every KNOWN_GAPS entry names the bead that owns it, so the checklist stays traceable."""
    for key, reason in KNOWN_GAPS.items():
        assert reason.startswith("phaze-"), f"KNOWN_GAPS{key} must start with the owning bead id, got {reason!r}"


def test_registries_have_no_stale_entries() -> None:
    """Classifications must still describe a real field/param, so the registries cannot rot.

    Without this, a field renamed or deleted leaves behind an entry that silently pre-approves a
    FUTURE field of the same name -- exactly how a suppression list turns into a blind spot.
    """
    live_body = {(schema.__name__, name) for schema, name, _a, _m, _c in _body_cases()}
    live_params = {(path, name) for path, name, _a, _m in _param_cases()}

    for schema, fields in UNMAPPED_BODY_FIELDS.items():
        columns = {c.key for c in sa_inspect(SCHEMA_BINDINGS[schema]).columns}
        for name in fields:
            assert (schema.__name__, name) in live_body, f"UNMAPPED_BODY_FIELDS[{schema.__name__}][{name!r}] names no live field"
            assert name not in columns, (
                f"UNMAPPED_BODY_FIELDS[{schema.__name__}][{name!r}] is stale -- {name} now maps to a real column, "
                f"so it is checked directly; delete the entry."
            )

    stale_params = PARAM_CLASSIFICATIONS.keys() - live_params
    assert not stale_params, f"PARAM_CLASSIFICATIONS entries name no live route param: {sorted(stale_params)}"

    stale_gaps = ALL_GAPS.keys() - live_body - live_params
    assert not stale_gaps, f"gap entries name no live field/param: {sorted(stale_gaps)}"


def test_unfiled_gaps_are_described_and_not_double_listed() -> None:
    """UNFILED_GAPS entries carry a reason and never shadow a filed bead's entry."""
    for key, reason in UNFILED_GAPS.items():
        assert reason and not reason.startswith("phaze-"), f"UNFILED_GAPS{key} names a bead ({reason!r}) -- it is filed now, move it to KNOWN_GAPS"
    overlap = KNOWN_GAPS.keys() & UNFILED_GAPS.keys()
    assert not overlap, f"listed in both KNOWN_GAPS and UNFILED_GAPS: {sorted(overlap)}"
