"""Order reads of one provider source object by a monotonic sequence, not wall-clock ``retrieved_at`` (phaze-z9ea3).

``provider_source_objects.read_seq`` is the last sequence handed out for the object, advanced under the object's row
lock. ``provider_source_reads`` appends one row per stored read; observations stay immutable (migration 085's trigger is
untouched). Existing observations are backfilled one read each, in (retrieved_at, id) order.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


revision: str = "091"
down_revision: str | None = "090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("provider_source_objects", sa.Column("read_seq", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_table(
        "provider_source_reads",
        sa.Column("object_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_source_objects.id"), primary_key=True),
        sa.Column("read_seq", sa.BigInteger(), primary_key=True),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("provider_source_observations.id"), nullable=False),
    )
    op.create_index("ix_provider_reads_observation", "provider_source_reads", ["observation_id", "read_seq"])
    op.execute(
        """
        INSERT INTO provider_source_reads (object_id, read_seq, observation_id)
        SELECT object_id, row_number() OVER (PARTITION BY object_id ORDER BY retrieved_at, id), id FROM provider_source_observations
        """
    )
    op.execute(
        """
        UPDATE provider_source_objects s SET read_seq = c.n
        FROM (SELECT object_id, count(*) AS n FROM provider_source_reads GROUP BY object_id) c
        WHERE s.id = c.object_id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_provider_reads_observation", table_name="provider_source_reads")
    op.drop_table("provider_source_reads")
    op.drop_column("provider_source_objects", "read_seq")
