"""Add the ``cloud_phase`` admission-progression column to ``cloud_job`` (Phase 55, D-04).

Additive-only, reversible migration. Extends the ``cloud_job`` sidecar with the single column the
Kueue admission view needs:

* ``cloud_phase`` (``String(20)``, NULL) -- the Kueue admission progression
  (``queued_behind_quota`` -> ``admitted`` -> ``running`` -> ``finished``). NULL for a1/local rows
  (admission is k8s-only); in-flight rows are stamped by submit + reconcile.

``cloud_phase`` is kept ORTHOGONAL to the existing ``inadmissible`` fault flag -- admission progress
and the fault flag are different concerns. The FileRecord state machine is untouched (KROUTE-03).

The CHECK (declared name ``cloud_phase_enum``, auto-prefixed to ``ck_cloud_job_cloud_phase_enum``
by the naming convention) is the authoritative membership gate for the string-backed ``CloudPhase``
StrEnum. The name is DISTINCT from ``status_enum`` so the two CHECKs never collide.

CRITICAL: this migration touches ONLY ``cloud_job``.
It must never reference ``saq_jobs`` (SAQ owns that table via init_db + saq_versions; an Alembic
migration touching it would collide -- 020 CRITICAL banner).

Revision ID: 027
Revises: 026
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "027"
down_revision: str | Sequence[str] | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CLOUD_PHASE_ENUM = "cloud_phase IN ('queued_behind_quota', 'admitted', 'running', 'finished')"


def upgrade() -> None:
    """Add the nullable cloud_phase column then its membership CHECK."""
    op.add_column("cloud_job", sa.Column("cloud_phase", sa.String(20), nullable=True))
    # Bare name ``cloud_phase_enum`` -- the ``ck_%(table_name)s_%(constraint_name)s`` naming convention
    # re-applies the ``ck_cloud_job_`` prefix (passing the already-prefixed name double-prefixes it).
    op.create_check_constraint("cloud_phase_enum", "cloud_job", _CLOUD_PHASE_ENUM)


def downgrade() -> None:
    """Drop the CHECK then the cloud_phase column (reverse order)."""
    op.drop_constraint("cloud_phase_enum", "cloud_job", type_="check")
    op.drop_column("cloud_job", "cloud_phase")
