"""Persisted operator priority flag for the 1001Tracklists drain (phaze-fq9h.8).

WHY THIS TABLE EXISTS
----------------------
``tasks.tracklist_drain.drain_tracklists`` has accepted ``flagged_file_ids`` since phaze-fq9h.7
and :class:`~phaze.services.tracklist_drain.DrainCandidate` sorts a flagged set ahead of
everything else -- but nothing wrote a flag anywhere durable. An operator's "answer this one
first" therefore lasted exactly as long as the single job it was passed into, which makes
"trigger/prioritize a lookup for a file" (this bead's own acceptance criterion) meaningless: the
very next drain slice -- run by a cron, a restart, or another click -- would forget the request
entirely.

One row per flagged file. No extra columns beyond the inherited ``created_at`` / ``updated_at``
(:class:`~phaze.models.base.TimestampMixin`): ``created_at`` is when the file was FIRST flagged,
``updated_at`` is when it was last (re-)flagged, and neither needs a bespoke column since the
admin UI writes both explicitly on every upsert (see
:func:`phaze.services.tracklist_priority.flag_file_for_lookup`).

LIFECYCLE
---------
A row is deleted when the operator explicitly un-prioritizes the file
(:func:`phaze.services.tracklist_priority.unflag_file`). It is deliberately NOT deleted
automatically the moment the drain answers the file: the drain's own queue-building step
(:func:`phaze.services.tracklist_drain.build_drain_queue`) already excludes any file whose unique
set is no longer queueable (found, cached negative, or otherwise resolved), so a stale flag on an
answered file has no effect on ordering -- it just sits inert until the operator clears it or
re-flags a still-open file. Auto-clearing on resolution would need to reconstruct "was this THIS
flag's file that got answered, or some completely different member of its unique set" at write
time, which is exactly the kind of extra bookkeeping the drain's cache-driven design (phaze-fq9h.3
/ .7) exists to avoid needing.
"""

from __future__ import annotations

import uuid  # noqa: TC003 -- SQLAlchemy resolves Mapped[] annotations at runtime

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class TracklistPriorityFlag(TimestampMixin, Base):
    """One row per file the operator has asked the drain to answer first.

    ``file_id`` is BOTH the primary key and the identity: flagging is a boolean per file, not an
    event log, so a second flag of the same file is an upsert (idempotent, re-stamps
    ``updated_at``) rather than a second row. ``ondelete="CASCADE"`` so a deleted file's flag
    cannot outlive it and silently reference nothing.
    """

    __tablename__ = "tracklist_priority_flags"

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE", name="fk_tracklist_priority_flags_file_id_files"),
        primary_key=True,
    )
