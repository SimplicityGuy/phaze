"""RuntimeConfigOverride model -- the DB override layer for hot-reloadable config (phaze-mvq8z.6).

A standalone app table (NOT part of SAQ's auto-managed ``saq_jobs``), one row per RELOADABLE key
currently overridden by an operator through the admin API/UI. This is the TOP layer of
``phaze.runtime_config``'s precedence ladder (``docs/design/0019-runtime-config-hot-reload.md`` §3):
a row present here always wins over the watched ``runtime.toml`` file and the process's env/default
layers, for that key alone. A key with no row here simply falls through to the next layer -- there
is no "unset" sentinel value, only row absence.

Mirrors :class:`~phaze.models.route_control.RouteControl` /
:class:`~phaze.models.pipeline_stage_control.PipelineStageControl`: a small, durable, directly
operator-mutable control table outside SAQ's schema, read through a degrade-safe service
(``phaze.services.runtime_config_overrides.get_runtime_config_overrides``) rather than raised
straight from the ORM.

``value`` is JSONB rather than a typed column because the override set spans multiple Python types
(``str`` for ``log_level``, ``int`` for every sizing/timeout key) across one table -- exactly the
shape :class:`~phaze.models.agent.Agent`'s ``last_status`` JSONB column already uses for a similarly
heterogeneous per-key blob. ``phaze.runtime_config.RuntimeConfig`` (``strict=True``) is what
actually enforces each key's type at reload time; this table stores whatever JSON-safe value the
admin API already validated before writing.

``created_at`` / ``updated_at`` come from :class:`TimestampMixin` (``updated_at`` carries
``onupdate=func.now()``) -- do not redeclare them here. ``updated_at`` doubles as "when this
override was last set", since a row is deleted (not soft-cleared) when an operator clears it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class RuntimeConfigOverride(TimestampMixin, Base):
    """One reloadable key's DB-override value (``key`` -> the override's current JSON value)."""

    __tablename__ = "runtime_config_override"

    # 64 matches route_control's/pipeline_stage_control's id/stage column width; every real
    # RuntimeConfig field name (phaze.runtime_config.RELOADABLE_KEYS) is well under it.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
