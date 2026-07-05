"""RouteControl model - a single durable row holding the force-local routing override (Phase 71).

A standalone one-row app table (NOT part of SAQ's auto-managed ``saq_jobs``) mirroring the
:class:`~phaze.models.pipeline_stage_control.PipelineStageControl` control-table pattern. It carries
exactly ONE row (PK ``id = 'global'``) whose ``force_local`` boolean is the runtime-mutable
incident-response switch: when engaged, every routing path behaves exactly like an all-local
registry (``cloud_enabled=False``) with no redeploy -- the drain (``stage_cloud_window``) becomes a
clean no-op and the duration router routes new long files LOCAL instead of holding them in
``AWAITING_CLOUD``.

The write SURFACE (thin endpoint + header pill) is Plan 04; this model + its degrade-safe reader
(``phaze.services.route_control.get_route_control``) + the two routing gates are the behavior change.

``created_at`` / ``updated_at`` come from :class:`TimestampMixin` (``updated_at`` carries
``onupdate=func.now()``) -- do not redeclare them here.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class RouteControl(TimestampMixin, Base):
    """Single-row force-local routing override (``id='global'``)."""

    __tablename__ = "route_control"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    force_local: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
