"""Typed SAQ-job payload models for file-bound tasks (Phase 26 D-22..D-24).

Every payload carries the MINIMUM data the agent needs to execute the job
without reading state back from the controller (D-23). `models_path` appears
only in ProcessFilePayload (essentia needs the .pb files); metadata/scan tasks
don't need it because their adapters point at local sidecars.

NO `current_path` per D-24 -- analysis / metadata / scan tasks work off
`original_path`, which was stamped at scan time and never changes.

THREE payloads take an explicit, narrow exception to that, because each addresses a
file that has ALREADY been moved, so `original_path` names nothing on disk:
`WriteFileTagsPayload.file_path`, `WriteCueSheetPayload.audio_path`, and
`ExecuteBatchProposalItem.source_path`. All three are ROLE names -- they say what the
path is FOR, not which column filled it -- and that is exactly what keeps them true
whichever column dispatch selects. `ExecuteBatchProposalItem.source_path` was named
`original_path` until phaze-xzjrr: phaze-shzdj made it carry `current_path` and the old
name then actively asserted the wrong thing, so the wire field was renamed while the
at-risk population was still zero (see its own docstring for the measurement and the
skew analysis). Each exception is documented where it is declared. `current_path` is
otherwise the post-execution path, sent back via patch_proposal_state.

WHAT D-24 RESTS ON, AND THE ASYMMETRY THAT ONCE BROKE IT (phaze-rhs6m, 2026-08-24).
D-24 is sound only while the three enrich producers can never be handed a file that
has already moved -- `original_path` is a valid on-disk location precisely until
execution flips it. That precondition is NOT enforced in the producers; it is a
PROPERTY OF THE PIPELINE ORDER, held up by the propose convergence gate
(`services/pipeline/proposals.py::_proposal_pending_clauses`), because nothing can be
executed that was not first proposed. The analyze and cloud-push seams hold it
structurally: an executed file has `analysis_completed_at` set, migration 033's
`analysis_completed_at XOR failed_at` CHECK makes done and failed mutually exclusive,
and every analyze/push trigger selects on one or the other -- and the one producer
that bypasses the `~done` gate, `services/reanalysis_backfill.py`, excludes applied
files by hand (`~applied_clause()`).

The METADATA seam did NOT hold, because the gate was ASYMMETRIC: analysis was gated on
a completion discriminator (Phase 57.1) while metadata was gated on BARE ROW EXISTENCE.
A metadata FAILURE is stored as a `metadata` row with `failed_at` set and payload NULL,
so a file whose metadata never landed cleared the gate, could be proposed / approved /
EXECUTED, and then -- since `done(metadata)` stays False until real metadata lands --
sat in the metadata pending set PERMANENTLY, where all four `ExtractMetadataPayload`
producers re-drive it at the `original_path` it had just been moved away from.

RESOLUTION -- the rule is UNCHANGED and the exception list does NOT grow. Operator
decision 2026-08-24 (phaze-rhs6m); question put as a choice among four mechanisms,
answer as given, verbatim and in full: "Close the gate asymmetry: require metadata
failed_at IS NULL". Durable record: the operator-decision comment on phaze-rhs6m. The
metadata conjunct now composes `done_clause(Stage.METADATA)`, so an executed file can
no longer BE metadata-pending and the producers keep shipping `original_path` under a
precondition that now actually holds. Shipping `current_path` from the three enrich
producers was considered and REFUSED -- it would have changed the read path for every
analysis and metadata job in the archive to fix one producer of three.

WHAT THIS DOES NOT COVER. The gate closes the state; it does not close a job that was
already enqueued against a still-eligible file and runs after the move (enqueue-then-
execute TOCTOU), nor `recover_orphaned_work` replaying a stored ledger payload whose
`original_path` was minted before the move. Both are INFERENCE FROM CODE, NOT MEASURED,
and are tracked separately as phaze-3542b, which carries that qualification verbatim. Do not
read this section as a claim that either is handled.

All schemas declare `extra="forbid"` per Phase 25 D-16 -- agent-supplied
job payloads are validated as strictly as HTTP request bodies.

Revision iteration 2 note (2026-05-12): ExecuteApprovedBatchPayload expanded
from `proposal_ids: list[UUID]` to a full `proposals: list[ExecuteBatchProposalItem]`
per checker B2 (user chose Option A: implement execute_approved_batch fully).
Each item carries the per-proposal data the agent needs to perform a local
file copy + verify + delete without DB access.
"""

from typing import Any, ClassVar
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProcessFilePayload(BaseModel):
    """SAQ job: CPU-bound essentia analysis of a single audio file."""

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    original_path: str
    file_type: str
    agent_id: str
    models_path: str  # essentia .pb files; only ProcessFile needs this
    # Phase 50 D-11: cloud push pipeline integrity + scratch read-path. The control plane pins
    # expected_sha256 from FileRecord.sha256_hash so the compute agent can verify the rsync'd
    # copy before analysis. `scratch_path is not None` is ITSELF the compute-read/ephemeral
    # signal (no separate boolean flag): when set, the worker reads/cleans up this ephemeral
    # copy instead of original_path. Both default None so the bulk local _enqueue_analysis_jobs
    # producer (five fields only) stays byte-identical under extra="forbid".
    expected_sha256: str | None = None
    scratch_path: str | None = None

    # ---- LEGACY-KEY SHIM (phaze-w55w1) — REMOVABLE, see the removal condition below ----
    @model_validator(mode="before")
    @classmethod
    def _drop_removed_window_cap_keys(cls, data: Any) -> Any:
        """Accept-and-discard the Phase 44 ``fine_cap`` / ``coarse_cap`` keys.

        These were the per-job "deepen" lever — a sentinel 0 disabling the window cap for one
        file. phaze-w55w1 removed the caps (ADR-0007 §7), so the keys mean nothing and the
        fields are gone.

        **Why tolerate them instead of letting ``extra="forbid"`` reject them.** Every
        ``process_file`` payload enqueued before this deploy is ALREADY SERIALIZED — in
        ``saq_jobs`` and, durably, in ``scheduling_ledger``. The old producer built the payload
        with ``model_dump(mode="json")`` and no ``exclude_none``, so *every* one of those rows
        carries ``"fine_cap": null, "coarse_cap": null`` — not just the handful that used the
        deepen lever. ``reenqueue._replay_row`` replays a ledger row's stored payload
        **verbatim**; it never regenerates it through the current producer. So rejecting the
        keys would not "route the job back through the current producer" — it would dead-letter
        the entire pre-upgrade analyze backlog on every recovery pass, permanently, with the
        ledger row surviving to try again next tick.

        Discarding is safe precisely because the values are now meaningless: a cap of 0 asked
        for the full window budget, which is what every file gets unconditionally.

        **Remove this shim when** no ``scheduling_ledger`` row (and no ``saq_jobs`` blob) written
        before the phaze-w55w1 deploy can still be replayed — i.e. once the analyze backlog that
        predates it has drained and been reaped. Until then it is load-bearing.
        """
        if isinstance(data, dict) and ("fine_cap" in data or "coarse_cap" in data):
            data = {k: v for k, v in data.items() if k not in ("fine_cap", "coarse_cap")}
        return data


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

    The agent needs full local-file-op context (source_path, proposed_path,
    proposed_filename, optional sha256 verify) in the payload itself -- D-23
    forbids reading state back from the controller mid-job.

    ``source_path`` is the absolute path the executor MOVES THE FILE FROM. It is a role
    name, not a column mirror: it says what the executor does with the value, so it stays
    true whichever column dispatch selects to fill it. Today dispatch fills it from
    ``FileRecord.current_path`` -- where the file is NOW -- and NOT from
    ``FileRecord.original_path`` (phaze-shzdj). ``FileRecord.original_path`` is written
    once at ingest and never again (operator, 2026-08-24: "original_path should never
    change. it's the ORIGINAL location of the file. the current_path is where the file
    is now."), so shipping it made the SECOND execution of an already-renamed file
    resolve a source that no longer exists.

    This is the same narrow, explicit D-24 exception :class:`WriteFileTagsPayload` and
    :class:`WriteCueSheetPayload` take, for the same reason: a file operation must
    address the file where it actually is. Everything the executor derives from this
    field is therefore current rather than stale -- the move SOURCE, the owning
    scan_root, AND the in-place-rename parent directory
    (``tasks.execution._resolve_destination``), which are all computed from this one
    value and so were all poisoned by the same staleness.

    THE FIELD WAS CALLED ``original_path`` UNTIL phaze-xzjrr, and the rename is a
    BREAKING WIRE CHANGE, deliberately taken while it was free. ``extra="forbid"`` makes
    the skew symmetric and, in both directions, LOUD AND PRE-FILE-OP:

    * an OLD payload (``original_path``) validated by NEW code fails
      :meth:`ExecuteApprovedBatchPayload.model_validate` at the top of
      ``tasks.execution.execute_approved_batch`` with two errors --
      ``extra_forbidden`` at ``proposals.N.original_path`` and ``missing`` at
      ``proposals.N.source_path``;
    * a NEW payload (``source_path``) validated by OLD agent code fails the mirror
      image of that.

    Either way validation precedes every file op, so nothing is half-moved and no
    ``ExecutionLog`` row is written -- a skewed deploy costs AVAILABILITY (the batch
    dead-letters and must be re-dispatched), never integrity. No compatibility shim is
    provided because none is owed: re-measured on the live catalog 2026-08-24 at
    implementation time, ``saq_jobs`` held 0 ``execute_approved_batch`` rows (of 13) and
    ``scheduling_ledger`` 0 rows with ``function = 'execute_approved_batch'``, so there
    is no serialized payload anywhere that a shim could rescue. Contrast
    :meth:`ProcessFilePayload._drop_removed_window_cap_keys`, which IS load-bearing
    precisely because that measurement was non-zero for it. RE-MEASURE BOTH TABLES
    BEFORE DEPLOYING A FURTHER RENAME -- the straight-rename shape is correct only while
    they are empty.

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
    source_path: str  # absolute move SOURCE; filled from FileRecord.current_path (D-24 exception)
    proposed_path: str  # RELATIVE destination directory ('' == rename in place)
    proposed_filename: str  # new filename incl. extension (appended under proposed_path)
    sha256_hash: str | None = None  # optional pre-copy integrity check


class WriteFileTagsPayload(BaseModel):
    """SAQ job: mutagen tag write + verify for ONE applied file, on the owning agent (phaze-6bkk).

    Deliberately carries ``file_path`` -- the post-execution ``FileRecord.current_path`` -- which
    the D-24 "no current_path in task payloads" convention normally forbids. The exception is
    explicit and narrow: a tag write is only ever offered for an APPLIED file (an executed
    proposal exists), so the file has already been moved and ``original_path`` no longer names
    anything on disk. ``current_path`` is exactly the state D-24 describes as "only meaningful
    AFTER execute_approved_batch flips state" -- which is precisely the state this task requires.

    ``log_id`` is the pre-minted ``TagWriteLog.id`` the control plane created in the ``queued``
    state. It makes the agent's result callback retry-stable: a SAQ retry PATCHes the SAME audit
    row instead of appending a duplicate (the ``execution_log_id`` discipline from Phase 28 D-15).
    """

    model_config = ConfigDict(extra="forbid")

    log_id: uuid.UUID
    file_id: uuid.UUID
    agent_id: str
    file_path: str
    # Values may be str / int / list[str] / None -- ``None`` DELETES the frame (phaze-52qd undo
    # semantics); ``list[str]`` (phaze-z2u08) writes a multi-value genre undo snapshot back as
    # separate frame/comment/atom values instead of collapsing it to one.
    tags: dict[str, str | int | list[str] | None]


class WriteCueSheetPayload(BaseModel):
    """SAQ job: write a fully-rendered CUE sheet next to its audio file, on the owning agent (phaze-6bkk).

    The CUE TEXT is generated on the control plane (``services.cue_generator.generate_cue_content``
    is pure string work over rows the api already has) and shipped whole, so the agent needs no DB
    access -- it only picks the next unused ``.cue`` / ``.vN.cue`` filename beside ``audio_path``
    and writes the bytes. Same ``current_path`` exception as
    :class:`WriteFileTagsPayload`: CUE generation is gated on the file being applied.
    """

    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    tracklist_id: uuid.UUID
    agent_id: str
    audio_path: str
    content: str


class CompanionReadItem(BaseModel):
    """One companion sidecar to read: its display filename and its on-agent path."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    path: str


class ReadCompanionFilesPayload(BaseModel):
    """SAQ job: bounded read of a media file's companion sidecars, on the owning agent (phaze-6bkk).

    Unlike every other agent task this one is REQUEST/RESPONSE -- ``services.proposal`` awaits its
    result via ``saq.Queue.apply`` rather than getting a callback. The controller worker is fileless
    under DIST-01, so the ``.nfo`` / ``.txt`` / ``.m3u`` context an LLM proposal wants can only be
    read where the mount is. Paths are agent-supplied strings; the agent re-runs the symlink-safe
    containment check against ITS OWN configured scan roots before opening anything.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    companions: list[CompanionReadItem] = Field(min_length=1, max_length=200)
    max_chars: int = Field(gt=0, le=1_000_000)


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
