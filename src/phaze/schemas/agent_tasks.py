"""Typed SAQ-job payload models for file-bound tasks (Phase 26 D-22..D-24).

Every payload carries the MINIMUM data the agent needs to execute the job
without reading state back from the controller (D-23). `models_path` appears
only in ProcessFilePayload (essentia needs the .pb files); fingerprint/metadata/
scan tasks don't need it because their adapters point at local sidecars.

NO `current_path` per D-24 -- agents work off `original_path` which was
stamped at scan time. `current_path` is the post-execution path; only
meaningful AFTER execute_approved_batch flips state, sent back via
patch_proposal_state (NOT carried in any task payload).

phaze-wsuf exception: `ScanLiveSetPayload.original_path` is populated from the file's
CURRENT on-disk location (`FileRecord.current_path`), not the scan-time original. The
scan-tab eligibility query offers a file for fingerprint scanning with no exclusion for an
already-executed (moved) file, so `original_path` can point at a path a prior execution
already deleted; `current_path` equals `original_path` until a move and is always the file's
live location. The field keeps its name (every other task's `original_path` still means the
scan-time path) -- only the VALUE producing it at the `POST /tracklists/scan` call site
differs, so the agent-side handler (`tasks/scan.py::scan_live_set`) needs no change.

All schemas declare `extra="forbid"` per Phase 25 D-16 -- agent-supplied
job payloads are validated as strictly as HTTP request bodies.

Revision iteration 2 note (2026-05-12): ExecuteApprovedBatchPayload expanded
from `proposal_ids: list[UUID]` to a full `proposals: list[ExecuteBatchProposalItem]`
per checker B2 (user chose Option A: implement execute_approved_batch fully).
Each item carries the per-proposal data the agent needs to perform a local
file copy + verify + delete without DB access.
"""

from typing import ClassVar
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    # Phase 50 D-11: cloud push pipeline integrity + scratch read-path. The control plane pins
    # expected_sha256 from FileRecord.sha256_hash so the compute agent can verify the rsync'd
    # copy before analysis. `scratch_path is not None` is ITSELF the compute-read/ephemeral
    # signal (no separate boolean flag): when set, the worker reads/cleans up this ephemeral
    # copy instead of original_path. Both default None so the bulk local _enqueue_analysis_jobs
    # producer (five fields only) stays byte-identical under extra="forbid".
    expected_sha256: str | None = None
    scratch_path: str | None = None


class PushFilePayload(BaseModel):
    """SAQ job: rsync-over-SSH push of a single media file to the compute scratch dir.

    Phase 50: enqueued by the bounded cloud-window cron and run on the fileserver agent
    (which owns the media mount). The deterministic-key builder reads `k["file_id"]`, so
    file_id must be present. `original_path` is the media-mount source the fileserver reads.
    """

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str

    # Phase 73 (D-01/D-02): the per-file rsync-push DESTINATION. dispatch (services/backends.py) stamps
    # these off the resolved ComputeBackend (record-don't-rederive) so the fileserver reads the RECORDED
    # target (Plan 02 rsync argv) rather than re-deriving it. Optional at the type level in this plan:
    # the dispatch producer supplies them (Task 3) but the /mismatch re-drive producer is wired in Plan
    # 03, so a four-field construction must still validate until then. They are NON-SECRET only (D-03):
    # host/scratch/user, never key material -- SSH keys/known_hosts stay agent-side.
    dest_host: str | None = None
    dest_scratch_dir: str | None = None
    dest_ssh_user: str | None = None

    # Phase 50 #sec argv-injection defense-in-depth: push_file hands original_path + file_type to
    # rsync as operands. A `--` terminator in the argv already blocks flag-smuggling, but reject
    # the dangerous shapes at the schema layer too (validated as strictly as an HTTP body).
    @field_validator("original_path")
    @classmethod
    def _original_path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("original_path must be an absolute path")
        return v

    @field_validator("file_type")
    @classmethod
    def _file_type_alnum(cls, v: str) -> str:
        if not v.isalnum():
            raise ValueError("file_type must be alphanumeric ([A-Za-z0-9]+)")
        return v

    @field_validator("dest_scratch_dir")
    @classmethod
    def _dest_scratch_absolute(cls, v: str | None) -> str | None:
        # Same shape as _original_path_absolute: the scratch dir is interpolated into the rsync remote
        # operand (`<dest_scratch_dir>/<file_id>.<ext>`), so a non-absolute value is rejected.
        if v is not None:
            if not v.startswith("/"):
                raise ValueError("dest_scratch_dir must be an absolute path")
            # WR-01: the scratch dir lands in the SAME ssh remote spec as dest_host/dest_ssh_user, so it
            # gets the same defense-in-depth shell-metacharacter guard (an absolute path never needs them).
            if any(ch in cls._DEST_HOST_FORBIDDEN for ch in v):
                raise ValueError("dest_scratch_dir must not contain whitespace or shell metacharacters")
        return v

    # Chars that must never reach the ssh remote spec / rsync operand: whitespace + shell metacharacters.
    # `--` terminators + list-argv (shell=False) already block flag-smuggling; this is defense-in-depth
    # at the schema layer (T-73-01), mirroring the original_path guard's intent.
    _DEST_HOST_FORBIDDEN: ClassVar[frozenset[str]] = frozenset(" \t\n\r;|&$`()<>")

    @field_validator("dest_host")
    @classmethod
    def _dest_host_safe(cls, v: str | None) -> str | None:
        if v is not None and any(ch in cls._DEST_HOST_FORBIDDEN for ch in v):
            raise ValueError("dest_host must not contain whitespace or shell metacharacters")
        return v

    @field_validator("dest_ssh_user")
    @classmethod
    def _dest_ssh_user_safe(cls, v: str | None) -> str | None:
        # Optional; when given it is a plain non-whitespace token (it also lands in the ssh remote spec).
        if v is not None and any(ch in cls._DEST_HOST_FORBIDDEN for ch in v):
            raise ValueError("dest_ssh_user must not contain whitespace or shell metacharacters")
        return v


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
    """SAQ job: fingerprint-query a live-set file and resolve a proposed tracklist.

    phaze-wsuf: `original_path` here is populated with the file's CURRENT on-disk location
    (`FileRecord.current_path`) by the `POST /tracklists/scan` handler, NOT the scan-time path
    -- see the module docstring's D-24 exception note. `combined_query` (tasks/scan.py) runs
    the fingerprint engines against exactly this path, so an executed (moved) file must be
    queried at its live location or the query targets a deleted path.
    """

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
    proposed_filename, optional sha256 verify) in the payload itself -- D-23
    forbids reading state back from the controller mid-job.

    ``proposed_path`` is the RELATIVE destination DIRECTORY the LLM proposed
    (e.g. ``"performances/artists/Disclosure"``), matching how it is stored on
    ``RenameProposal.proposed_path`` and joined in ``services.collision`` as
    ``concat(proposed_path, '/', proposed_filename)``. It is NOT an absolute
    destination file path; the executor resolves it against the owning
    scan_root and appends ``proposed_filename``. An empty string means "rename
    in place" (keep the current directory, apply the new filename).

    ``proposed_filename`` is the new filename (with extension). It is always
    present on the wire because ``RenameProposal.proposed_filename`` is
    non-nullable -- carrying it here is what lets the executor build the real
    destination instead of treating the relative directory as an absolute file
    (the bug that failed every approved proposal at ``failed_at_step='copy'``).
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: uuid.UUID
    file_id: uuid.UUID
    original_path: str
    proposed_path: str  # RELATIVE destination directory ('' == rename in place)
    proposed_filename: str  # new filename incl. extension (appended under proposed_path)
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
