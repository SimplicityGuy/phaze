"""FileRecord model - central file record.

Phase 90 (MIG-04): the ``FileState`` StrEnum, the ``files.state`` column, and the
``ix_files_state`` index were removed. A file's stage/status is now DERIVED entirely from its
output tables (``analysis`` / ``metadata`` / ``proposals`` markers, the
``cloud_job`` sidecar, and the ``dedup_resolution`` marker) via ``services/stage_status.py``. The
irreversible column drop shipped in migration ``039_drop_files_state_column``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from phaze.models.base import Base, TimestampMixin


if TYPE_CHECKING:
    from phaze.models.metadata import FileMetadata


class FileRecord(TimestampMixin, Base):
    """Central file record tracking each file through the processing pipeline.

    phaze-48ghg.7: repowise's health index flags this file as 54% duplicated (``dry_violation``,
    4 clone pairs, worst pair 15 lines shared with :class:`~phaze.models.filename_convention.
    FilenameConvention`). Checked against the actual bytes rather than the score: every "cloned"
    line is the standard SQLAlchemy declarative shape this repo uses for EVERY model -- an ``id``
    primary key column plus a handful of ``name: Mapped[T] = mapped_column(...)`` typed-column
    declarations. The exact ``id`` line below appears verbatim in 19 of the 24 files under
    ``models/``, and the ``agent_id`` foreign-key block a few lines down is byte-identical to
    :class:`~phaze.models.scan_batch.ScanBatch`'s. It is schema-declaration syntax, not shared
    business logic: there is no behavior that could drift out of sync (consistent with the worst
    clone partner's co-change count of 0 -- the two files have never needed synchronized
    maintenance). Extracting a mixin would relocate one obvious, self-documenting line behind an
    extra layer of indirection across up to 19 files for zero behavior change -- the "abstraction
    costs more than it saves" case this repo's own convention warns against.
    :class:`~phaze.models.base.TimestampMixin` remains the bar for what earns extraction here: it
    exists because of real, non-obvious runtime behavior (see its docstring), not because two
    models happen to declare columns the same way. Reasoned no-change.
    """

    # phaze-48ghg.7: repowise's health index also flags a ``hidden_coupling`` with
    # ``phaze.tasks.controller`` -- 3 shared commits, 60% of that file's co-changes, with NO static
    # dependency (``controller.py`` never imports this model). Checked against the actual git
    # history rather than the score: every shared commit is a "Phase N" pipeline-feature landing
    # (e.g. Phase 49 duration routing, Phase 69 tiered drain scheduling) that adds a new pipeline
    # stage -- which means BOTH a new tracked-state signal here (a column, or previously a
    # ``FileState`` member before Phase 90's MIG-04 removed that enum) AND a new task function that
    # ``controller.py`` must register in its ``settings["functions"]`` / ``settings["cron_jobs"]``
    # lists (see that module). ``controller.py`` never needs to import ``FileRecord`` directly --
    # it is a composition root that wires together the task modules under ``phaze.tasks`` which DO
    # read/write this table -- so the coupling is real but travels through an intermediate layer a
    # static import graph cannot see. Recorded here rather than invented as a fake import.
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    # phaze-x4ux: derived, mojibake-repaired copy of `original_filename`, populated ONCE at
    # ingest (routers/agent_files.py::upsert_files) when the repair actually changes the text;
    # NULL otherwise, so `COALESCE(original_filename_repaired, original_filename)` always yields
    # the best-known-clean display/search text. `original_filename` itself is NEVER rewritten --
    # it is the byte-faithful record of what is actually on disk; renaming the file is the
    # separate, human-approved rename-proposal workflow's job, not this column's.
    original_filename_repaired: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_type: Mapped[str] = mapped_column(String(10), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("scan_batches.id"), nullable=True)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )

    file_metadata: Mapped[FileMetadata | None] = relationship("FileMetadata", foreign_keys="FileMetadata.file_id", uselist=False, lazy="noload")

    __table_args__ = (
        Index("ix_files_sha256_hash", "sha256_hash"),
        Index("uq_files_agent_id_original_path", "agent_id", "original_path", unique=True),
        # phaze-bto9 (migration 048): the ordered index the tag-write review keyset paging needs.
        # ``services.review.get_tagwrite_review_page`` does ``ORDER BY original_filename, id`` plus a
        # ``(original_filename, id) > (:last_name, :last_id)`` range; the only other index touching
        # ``original_filename`` is a GIN trgm one, which can serve neither an ordered scan nor a row
        # range -- so every batch re-scanned and re-sorted the whole table without this.
        Index("ix_files_original_filename_id", "original_filename", "id"),
    )
