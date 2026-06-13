"""Add pipeline_stage_control table (per-stage pause/priority operator intent).

Additive-only migration for Phase 37. Creates the new standalone app table
``pipeline_stage_control`` and seeds exactly 3 rows -- one per agent pipeline
stage (``metadata`` / ``analyze`` / ``fingerprint``) -- with the resume/pause
baseline (``paused=false``) and the default dequeue priority (``priority=50``).

The table is the durable home for operator intent (pause + priority per stage).
A DB CHECK ``priority BETWEEN 0 AND 100`` keeps every stage priority inside SAQ's
dequeue window (``priority BETWEEN 0 AND 32767``) so a stage can never be driven
silently un-dequeueable at the schema layer, even if endpoint clamping is bypassed
(threat T-37-02).

CRITICAL: this migration touches ONLY ``pipeline_stage_control``. It must NEVER
reference ``saq_jobs`` -- SAQ owns that table via its own ``init_db()`` +
``saq_versions`` and an Alembic migration touching it would collide (37-RESEARCH
Anti-Patterns).

No data migration of ``saq_jobs`` -- this migration only creates+seeds the new
app table; live queue rows are mutated at runtime by the control endpoints.

Revision ID: 020
Revises: 019
Create Date: 2026-06-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "020"
down_revision: str | Sequence[str] | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The three agent pipeline stages seeded at upgrade time. A bound static tuple --
# no user input is interpolated into the seed INSERT (threat T-37-01).
_SEED_STAGES = ("metadata", "analyze", "fingerprint")


def upgrade() -> None:
    """Create pipeline_stage_control with the priority CHECK, then seed 3 stage rows."""
    op.create_table(
        "pipeline_stage_control",
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("50"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("stage", name=op.f("pk_pipeline_stage_control")),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name=op.f("ck_pipeline_stage_control_priority_range")),
    )
    # Seed the 3 stage rows with the pause/resume baseline (paused=false, priority=50).
    # One execute per stage, bound params only -- NO f-string interpolation of the
    # stage label into the SQL (threat T-37-01; mirrors the 012/019 idiom).
    bind = op.get_bind()
    for stage in _SEED_STAGES:
        bind.execute(
            sa.text("INSERT INTO pipeline_stage_control (stage, paused, priority, created_at, updated_at) VALUES (:stage, false, 50, NOW(), NOW())"),
            {"stage": stage},
        )


def downgrade() -> None:
    """Drop pipeline_stage_control (mirror of upgrade)."""
    op.drop_table("pipeline_stage_control")
