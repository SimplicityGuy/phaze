"""Tag-write-status enum (DB-free).

Lives outside :mod:`phaze.models` so that the agent-side tag-write task
(:mod:`phaze.tasks.tag_write`, loaded inside the agent worker process) and the
``phaze.schemas.agent_tag_writes`` wire contract can name the same statuses without
transitively pulling in SQLAlchemy / :mod:`phaze.database` (Phase 26 D-03 / D-25).

:mod:`phaze.models.tag_write_log` re-imports and re-exports this symbol so legacy
imports ``from phaze.models.tag_write_log import TagWriteStatus`` keep working.
"""

from __future__ import annotations

import enum


class TagWriteStatus(enum.StrEnum):
    """Status of a tag write operation.

    The ``status`` column is a plain ``String(20)`` (no PG enum / CHECK constraint), so
    adding a value here needs no migration.
    """

    # phaze-6bkk: the audit row exists but the disk write has not happened yet -- the control
    # plane has enqueued ``write_file_tags`` onto the owning agent's meta lane and is waiting for
    # the agent's callback to move the row to a terminal status. DIST-01 makes this the ONLY
    # possible first state for a write: the api container has no media mount, so it can never
    # perform the mutagen write itself. Deliberately NON-terminal for the idempotency anti-join
    # (``_TERMINAL_TAGWRITE_STATUSES``) -- a queued row must not evict its file from the tag-write
    # candidate window, or an agent that never reports would silently drop the file forever.
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"
    DISCREPANCY = "discrepancy"
    # phaze-vq3g: the on-disk write SUCCEEDED but the immediate verify re-read failed (transient
    # I/O / mutagen re-parse error). Distinct from DISCREPANCY (write landed the WRONG values) and
    # from FAILED (the write itself never landed): here the file is correctly tagged but could not
    # be confirmed, so it must NOT be audited as an all-field ``actual=None`` discrepancy. Like
    # FAILED/DISCREPANCY it is intentionally NON-terminal, so the file resurfaces and a later submit
    # re-verifies and self-heals to COMPLETED once the transient condition clears.
    VERIFY_FAILED = "verify_failed"
    # WR-01: a terminal marker for an applied file whose server-computed proposal has ZERO changes
    # (nothing to write). It is NOT an audio write -- it records that the file was inspected and
    # needs no tag write, so the idempotency anti-join (``completed``/terminal subquery) EVICTS it
    # from the candidate window and it can never re-occupy the alphabetically-first ``.limit()``
    # slots and starve qualifying files.
    NO_OP = "no_op"
