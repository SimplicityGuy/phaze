"""Typed SAQ-job payload models for file-bound tasks (Phase 26 D-22..D-24).

Every payload carries the MINIMUM data the agent needs to execute the job
without reading state back from the controller (D-23). `models_path` appears
only in ProcessFilePayload (essentia needs the .pb files); fingerprint/metadata/
scan tasks don't need it because their adapters point at local sidecars.

NO `current_path` per D-24 -- agents work off `original_path` which was
stamped at scan time. `current_path` is the post-execution path; only
meaningful AFTER execute_approved_batch flips state, sent back via
patch_proposal_state (NOT carried in any task payload).

All schemas declare `extra="forbid"` per Phase 25 D-16 -- agent-supplied
job payloads are validated as strictly as HTTP request bodies.

Revision iteration 2 note (2026-05-12): ExecuteApprovedBatchPayload expanded
from `proposal_ids: list[UUID]` to a full `proposals: list[ExecuteBatchProposalItem]`
per checker B2 (user chose Option A: implement execute_approved_batch fully).
Each item carries the per-proposal data the agent needs to perform a local
file copy + verify + delete without DB access.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class ProcessFilePayload(BaseModel):
    """SAQ job: CPU-bound essentia analysis of a single audio file."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str
    models_path: str  # essentia .pb files; only ProcessFile needs this
    # Phase 44: optional per-job analysis cap overrides (the "deepen analysis" lever).
    # Default None preserves the bulk _enqueue_analysis_jobs producer (five fields only) under
    # extra="forbid". When set, the worker prefers these over the AgentSettings 60/30 defaults;
    # a cap of 0 reaches analysis.py::_stride_to_cap as the analyze-ALL-windows no-op (unbounded).
    fine_cap: int | None = None
    coarse_cap: int | None = None


class ExtractMetadataPayload(BaseModel):
    """SAQ job: mutagen tag-extraction for a single audio/video file."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str


class FingerprintFilePayload(BaseModel):
    """SAQ job: submit a file to audfprint + panako sidecars."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    agent_id: str


class ScanLiveSetPayload(BaseModel):
    """SAQ job: fingerprint-query a live-set file and resolve a proposed tracklist."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    agent_id: str


class ScanDirectoryPayload(BaseModel):
    """SAQ job: walk a directory on the agent and stream FileRecord chunks back via HTTP (Phase 27 D-14).

    Carries the per-job snapshot the agent needs to walk `scan_path`, post
    chunks of FileUpsertRecord to `POST /api/internal/agent/files` (binding
    each chunk to `batch_id`), and PATCH the batch progress + final status.
    D-23 forbids reading state back from the controller mid-job; everything
    the agent needs is in this payload.
    """

    model_config = ConfigDict(extra="forbid")

    scan_path: str
    batch_id: uuid.UUID
    agent_id: str


class ExecuteBatchProposalItem(BaseModel):
    """Per-proposal details carried inside ExecuteApprovedBatchPayload.proposals.

    The agent needs full local-file-op context (original_path, proposed_path,
    optional sha256 verify) in the payload itself -- D-23 forbids reading
    state back from the controller mid-job.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: uuid.UUID
    file_id: uuid.UUID
    original_path: str
    proposed_path: str
    sha256_hash: str | None = None  # optional pre-copy integrity check


class ExecuteApprovedBatchPayload(BaseModel):
    """SAQ job: per-agent sub-batch of an approved-proposal execution dispatch.

    Carries everything the agent needs to perform local file operations and
    report per-proposal results back via PATCH /proposals/{id}/state.
    Cross-proposal failures are isolated: one bad file does NOT fail the batch.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    agent_id: str
    proposals: list[ExecuteBatchProposalItem] = Field(min_length=1, max_length=500)
    sub_batch_index: int = 0  # Phase 28 D-10 -- 0-based; default preserves legacy callers
