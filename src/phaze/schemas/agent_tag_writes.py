"""Wire contract for the agent -> control tag-write result callback (phaze-6bkk).

``PATCH /api/internal/agent/tag-writes/{log_id}`` is how the file-server agent reports the
outcome of a ``write_file_tags`` job back to the control plane, which owns the ``tag_write_log``
audit table. Mirrors the ``agent_execution`` / ``agent_metadata`` callback shape: the agent never
touches Postgres (D-25), so every durable effect of an on-disk write lands through this endpoint.

``extra="forbid"`` per Phase 25 D-16 -- agent-supplied bodies are validated as strictly as any
other HTTP request body.
"""

from __future__ import annotations

import uuid  # noqa: TC003 -- pydantic resolves annotations at runtime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from phaze.enums.tag_write import TagWriteStatus  # noqa: TC001  -- pydantic resolves this annotation at runtime
from phaze.services.pg_text import find_pg_unsafe_json_reason


def _reject_pg_unsafe_json(v: dict[str, object] | None, info: ValidationInfo) -> dict[str, object] | None:
    """Shared field_validator body for a ``dict`` field bound for a ``jsonb`` column (phaze-hvve5).

    ``before_tags``/``discrepancies`` land straight into JSONB columns with no per-key/value
    sanitization -- unlike a scalar ``error_message`` field, :func:`sanitize_pg_text` cannot run on
    a whole dict, and a NUL/lone-surrogate nested in a key or value passes Pydantic validation but
    aborts the callback's ``session.commit()`` with ``CharacterNotInRepertoireError`` AFTER the
    on-disk tag write already landed (compounds phaze-anrw4 / phaze-yy9bk: the row is stranded, not
    just delayed). REJECT (422) rather than silently strip a key/value out of a snapshot dict --
    that would make the persisted ``before_tags`` disagree with what the agent actually read off
    disk, corrupting the undo anchor the docstrings above describe.
    """
    reason = find_pg_unsafe_json_reason(v)
    if reason is not None:
        raise ValueError(f"{info.field_name} {reason}")
    return v


# Bound the persisted failure detail to the same wire bound the metadata failure callback uses.
# The column is unbounded ``Text``, so the cap is the DoS guard, not a column-width echo.
_ERROR_MESSAGE_MAX = 2000


class TagWriteResultPayload(BaseModel):
    """Terminal outcome of one on-agent tag write.

    ``status`` is deliberately typed as the shared :class:`TagWriteStatus` minus its non-terminal
    ``queued`` member: a callback that re-asserted ``queued`` would strand the row forever, so the
    router rejects it (422) rather than persisting a no-progress update.
    """

    model_config = ConfigDict(extra="forbid")

    status: TagWriteStatus
    # phaze-52qd: the COMPLETE before/undo snapshot the agent read off disk immediately before
    # writing -- every core field, ``None`` where the tag was absent. This is the only place it can
    # be captured (the control plane cannot read the file), and it is what an undo re-applies.
    before_tags: dict[str, str | int | list[str] | None] = Field(default_factory=dict)
    discrepancies: dict[str, dict[str, str | None]] | None = None
    error_message: str | None = Field(default=None, max_length=_ERROR_MESSAGE_MAX)
    # phaze-2zeu0: the file's sha256 as the agent OBSERVED IT ON DISK after doing its work. A tag
    # write rewrites the file's bytes, so ``FileRecord.sha256_hash`` -- written once at ingest
    # (``tasks/scan.py:218``) and never refreshed -- goes stale the moment a write lands, and every
    # consumer that verifies bytes against that column then fails PERMANENTLY: the execution
    # pre-copy verify (``tasks/execution.py``) raises "sha256 mismatch", and the cloud lane's
    # integrity gate (``job_runner._verify_integrity_step``) exits 11 with no retry.
    #
    # This is deliberately typed as an OBSERVATION, not as an invalidation signal, and that framing
    # is what makes it correct for every status rather than just the happy one. The agent is the
    # only party that can read the file (DIST-01: the api container has no media mount), so it
    # reports what is actually there; the control plane records it. ``None`` means "not observed"
    # (the containment refusal returns before any disk I/O), NOT "unchanged" -- so the control
    # plane leaves the column alone rather than guessing.
    #
    # Same lowercase-hex bound as ``PresignDownloadResponse.expected_sha256`` and the
    # ``FileRecord.sha256_hash`` column (``String(64)``); ``compute_sha256`` returns exactly that.
    sha256_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _reject_pg_unsafe_before_tags = field_validator("before_tags", "discrepancies", mode="after")(_reject_pg_unsafe_json)


class TagWriteResultResponse(BaseModel):
    """Ack for a tag-write result callback."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    log_id: uuid.UUID
    status: TagWriteStatus
    # False when the row was already terminal and this callback was a duplicate/late replay --
    # the endpoint is idempotent, so a replay is a 200 no-op, never an error.
    applied: bool


class TagWriteBeforeSnapshotPayload(BaseModel):
    """The pre-write on-disk snapshot, reported SEPARATELY and BEFORE any mutating write (phaze-anrw4).

    ``PATCH /api/internal/agent/tag-writes/{log_id}/before-snapshot`` exists to close a gap the
    main result callback cannot: on a SAQ retry of ``write_file_tags`` (the job body raises on a
    failed result-callback so SAQ retries -- "only the CALLBACK is allowed to fail the job"), the
    first attempt's write can have already landed on disk even though its result callback never
    reached the control plane. A second attempt's ``_extract_before_tags`` then reads the
    ALREADY-WRITTEN state, not the true original -- and the row is still ``queued``, so the main
    callback's ``status != queued`` duplicate guard does not catch it either.

    Reporting the snapshot here, before the disk is touched, makes it independent of which
    attempt's write or result callback actually lands: the control plane accepts it FIRST-WRITE-WINS
    (whichever call reaches it first while ``TagWriteLog.before_tags`` is still empty), so the
    corrupted second extraction can never overwrite an already-captured original.
    """

    model_config = ConfigDict(extra="forbid")

    before_tags: dict[str, str | int | list[str] | None] = Field(default_factory=dict)

    # Same sink, same hazard as `TagWriteResultPayload.before_tags` -- see `_reject_pg_unsafe_json`.
    _reject_pg_unsafe_before_tags = field_validator("before_tags", mode="after")(_reject_pg_unsafe_json)


class TagWriteBeforeSnapshotResponse(BaseModel):
    """Ack for a before-snapshot report."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    log_id: uuid.UUID
    # False when a snapshot was already recorded for this row (first-write-wins) or the row has
    # already left ``queued`` -- never an error, always a safe no-op.
    applied: bool
