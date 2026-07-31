"""DeclarativeBase with naming conventions and timestamp mixin."""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models with naming conventions."""

    metadata = MetaData(naming_convention=convention)


class TimestampMixin:
    """Mixin providing timezone-AWARE created_at and updated_at timestamp columns.

    ``DateTime(timezone=True)`` is load-bearing, not decoration (phaze-cz3m). A bare
    ``Mapped[datetime]`` renders as ``TIMESTAMP WITHOUT TIME ZONE``, and SQLAlchemy's asyncpg
    dialect emits that as an explicit ``$n::TIMESTAMP WITHOUT TIME ZONE`` cast on every bind
    param typed off the column. asyncpg decodes by column OID, so any column that is genuinely
    ``timestamptz`` in Postgres comes back tz-AWARE regardless of what the model claims -- and
    feeding such a value back in through a naive-typed param raises
    ``DataError: can't subtract offset-naive and offset-aware datetimes`` inside asyncpg's
    ``timestamp_encode``.

    That round-trip took the cloud staging scheduler down for ~10 h: the keyset cursor in
    ``services.pipeline.get_cloud_staging_candidates`` reads ``files.created_at`` (aware) and
    binds it back as ``literal(value, FileRecord.created_at.type)`` (declared naive). Migration
    049 makes every timestamp column in the schema ``timestamptz``, so this declaration is now
    the truth for every table rather than a per-table coin flip; the schema-wide guard in
    ``tests/integration/test_migrations/test_baseline_schema.py`` fails the build if either side
    drifts back.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
