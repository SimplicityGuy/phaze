"""Host-observed identity of a running Phaze container."""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base


class Deployment(Base):
    """One container, keyed by Docker's immutable container ID rather than a lane name."""

    __tablename__ = "deployments"

    container_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    host: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    lane: Mapped[str | None] = mapped_column(String(32))
    app_version: Mapped[str | None] = mapped_column(String(64))
    image_ref: Mapped[str | None] = mapped_column(String(256))
    image_digest: Mapped[str | None] = mapped_column(String(71))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
