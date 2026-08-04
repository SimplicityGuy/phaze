"""FilenameConvention model -- corpus-learned filename conventions, keyed generically (phaze-5fta.2).

WHY A GENERALIZED STORE, NOT A DATE FEATURE
--------------------------------------------
The first payload is date order (DD-MM vs MM-DD), measured on the live archive: of 6,910
filenames carrying a ``\\d{2}-\\d{2}-\\d{4}`` date, 2,430 (35.2%) are individually ambiguous, but
grouping the self-resolving remainder by trailing scene release-group tag shows unanimous
per-group conventions with hard supporting/contradicting evidence (see the phaze-5fta epic
description for the full finding). Date order is deliberately treated as the FIRST convention
kind, not THE feature: the epic's design explicitly calls for future kinds (field ordering,
separator style, source-tag vocabulary, episode placement) and future scopes (``directory``,
``agent``, ``global`` alongside today's ``release_group``) to slot in without a schema change.
That is why ``scope`` and ``convention_kind`` are free-form strings with NO enum ``CHECK`` --
mirroring the ``tracklist_lookup_cache.outcome`` precedent (a third-party taxonomy that grows is
not a migration-worthy CHECK) -- and why there is no ``date_order`` / ``dd_mm`` column anywhere
on this table. A caller wanting to add "separator style" tomorrow inserts a row with
``convention_kind='separator_style'``; nothing here changes.

THE TABLE IS A CACHE, NEVER HAND-EDITED
-----------------------------------------
Every row is fully rebuildable from the files corpus. The learner (phaze-5fta.3) is a
full-refresh recompute, operator-triggered -- there is no incremental-update code path to keep
consistent, and no row here is a source of truth for anything the files themselves don't already
say.

CONFIDENCE IS DERIVED, NEVER HAND-SET
---------------------------------------
``confidence`` is a Postgres ``GENERATED ALWAYS AS (...) STORED`` column (:class:`sqlalchemy.Computed`,
``persisted=True``), computed from ``supporting_count`` / ``contradicting_count`` alone:
``supporting / (supporting + contradicting)``, or ``0.0`` when there is no supporting-or-
contradicting evidence yet. This is a DB-level guarantee, not an application convention: Postgres
rejects any INSERT/UPDATE that names ``confidence`` explicitly (``ERROR: column "confidence" can
only be updated to DEFAULT``), so no caller -- ORM, raw SQL, or a future admin tool -- can write a
confidence value that disagrees with the counts it was derived from. ``ambiguous_count`` is
DELIBERATELY excluded from the formula: it tallies how many individually-ambiguous filenames in
this ``(scope, scope_value)`` would benefit from the convention being applied (the payoff size),
not evidence for or against the convention itself -- see the epic's PRECEDENCE RULES (a
self-resolving file always wins; ambiguous files are the fallback target, never a vote).

UNIQUE (scope, scope_value, convention_kind)
----------------------------------------------
One row per (dimension, dimension-value, aspect) triple -- e.g. at most one ``date_order`` row for
release group ``talion``. A learner re-run upserts existing rows rather than duplicating them.

``created_at`` / ``updated_at`` come from :class:`TimestampMixin` -- do not redeclare.
``computed_at`` is a SEPARATE, explicitly-written timestamp (the ``dedup_resolution.resolved_at``
precedent): it is the wall-clock moment the LEARNER computed this row's evidence, not merely "the
last time this row was touched" -- the two coincide today (write-once per full refresh) but are
conceptually distinct and must not be collapsed into ``updated_at``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- SQLAlchemy resolves Mapped[] annotations at runtime
import uuid

from sqlalchemy import CheckConstraint, Computed, DateTime, Float, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


# The DB-enforced derivation: supporting share of (supporting + contradicting) evidence, 0.0 when
# there is none yet. Kept as a module-level constant so the ORM ``Computed(...)`` expression and
# the Alembic migration's ``sa.Computed(...)`` render the IDENTICAL SQL text -- a single source of
# truth for the one string that must never drift between the two.
CONFIDENCE_EXPRESSION = (
    "CASE WHEN (supporting_count + contradicting_count) > 0 "
    "THEN supporting_count::double precision / (supporting_count + contradicting_count) "
    "ELSE 0.0 END"
)


class FilenameConvention(TimestampMixin, Base):
    """One row per corpus-learned convention for a (scope, scope_value, convention_kind) triple."""

    __tablename__ = "filename_convention"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The dimension the convention is keyed on. 'release_group' is the only value the learner
    # (phaze-5fta.3) writes today; 'directory' / 'agent' / 'global' are epic-design future scopes.
    # No enum CHECK -- see module docstring; the taxonomy is expected to grow without a migration.
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    # The value within that scope, e.g. the release-group tag ('talion'). Text, not a bounded
    # String, because a future 'directory' scope's value is an archive path with no fixed cap.
    scope_value: Mapped[str] = mapped_column(Text, nullable=False)
    # The aspect being learned, e.g. 'date_order' (the only kind the learner writes today).
    # No enum CHECK -- same reasoning as `scope` above; field ordering, separator style,
    # source-tag vocabulary and episode placement all slot in as new values, not new columns.
    convention_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    # The learned value itself, e.g. 'MM-DD'. NULLABLE: the learner may tally evidence for a
    # (scope_value, convention_kind) pair without a clear winner (e.g. a genuine 0-0 or tied
    # supporting/contradicting split) -- the row still records that the question was asked.
    convention_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Evidence counts -- the ONLY inputs `confidence` is derived from (contradicting/supporting)
    # plus the payoff-sizing `ambiguous_count`, which is deliberately NOT part of the formula
    # (see module docstring).
    supporting_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    contradicting_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # DERIVED, NEVER HAND-SET: a Postgres GENERATED ALWAYS AS (...) STORED column. Postgres itself
    # rejects any attempt to write this column directly -- see module docstring.
    confidence: Mapped[float] = mapped_column(Float, Computed(CONFIDENCE_EXPRESSION, persisted=True), nullable=False)

    # The wall-clock moment the learner's full-refresh recompute produced this row. Distinct from
    # `updated_at` (see module docstring) even though the two coincide today.
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "scope",
            "scope_value",
            "convention_kind",
            name="uq_filename_convention_scope_scope_value_convention_kind",
        ),
        CheckConstraint(
            "supporting_count >= 0 AND contradicting_count >= 0 AND ambiguous_count >= 0",
            name="counts_non_negative",
        ),
    )
