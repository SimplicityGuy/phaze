"""ExecutionLog model - append-only audit trail for file operations.

``ExecutionStatus`` is re-exported from :mod:`phaze.enums.execution` so the
canonical definition can live in a DB-free module. Schemas under
``phaze.schemas.agent_*`` import the enum from the DB-free location without
transitively dragging in SQLAlchemy / the ORM Base (Phase 26 D-03 / Plan 11).
"""

from datetime import datetime
import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.enums.execution import ExecutionStatus
from phaze.models.base import Base, TimestampMixin


# Re-export so legacy `from phaze.models.execution import ExecutionStatus` keeps working.
__all__ = ["ExecutionLog", "ExecutionStatus"]


class ExecutionLog(TimestampMixin, Base):
    """Append-only audit log for file rename/move operations."""

    __tablename__ = "execution_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("proposals.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    destination_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(server_default=func.now())
