"""Add the derived-status substrate: failure markers, dedup marker, cloud sidecar (Phase 77, MIG-01/PERF-01).

Additive-only, forward-focused migration. It lands the schema objects the Parallel Enrich DAG
milestone's later derivation phases READ, and backfills them (READ-ONLY) from ``files.state`` --
NOTHING in Phase 77 reads them yet. ``032.upgrade()`` touches ONLY ``analysis``, ``metadata``,
``dedup_resolution`` and ``cloud_job``:

* ``analysis`` / ``metadata`` gain nullable ``failed_at`` (``TIMESTAMPTZ``) + ``error_message``
  (``Text``) failure-marker columns (D-01) on the existing 1:1 output tables (NOT a generic
  ``stage_failure`` table -- preserves the <=1-row-per-file invariant).
* New ``dedup_resolution(file_id UNIQUE FK, canonical_file_id FK NULL, resolved_at)`` 1:1 sidecar
  (D-07) -- marker-row existence = resolved; undo = DELETE the row.
* ``cloud_job`` ``status`` CHECK widened to admit ``'awaiting'`` (D-04); an AWAITING_CLOUD file is
  represented by a ``cloud_job`` row ``status='awaiting'`` (``s3_key``/``upload_id`` NULL).
* Five partial indexes mirroring the ORM ``__table_args__`` byte-for-byte (PERF-01 empty-diff).

Backfill (set-based static SQL, one statement per object, READ-ONLY on ``files.state``):
  * analyze-failed: UPSERT ``analysis.failed_at`` for every ``state='analysis_failed'`` file --
    an ``INSERT..SELECT..ON CONFLICT (file_id) DO UPDATE`` because ``report_analysis_failed`` writes
    NO ``analysis`` row, so a failed file may have no row (RESEARCH Pitfall 2). ``analysis_completed_at``
    stays NULL for these rows.
  * dedup: one ``dedup_resolution`` row per ``state='duplicate_resolved'`` file, deriving
    ``canonical_file_id`` deterministically (``ORDER BY c.id LIMIT 1`` among non-resolved same-sha256
    members; NULL if none -- RESEARCH Pitfall 4).
  * cloud awaiting/pushing/pushed: gap-fill a ``cloud_job`` row (``awaiting``/``uploading``/``uploaded``)
    only for files missing one (D-04/D-06). LOCAL_ANALYZING gets NO sidecar row (D-05, derived later).

Downstream-reader callouts (honored in the derivation phase, NOT here):
  * D-02: the future ``done(metadata)`` predicate tightens to
    ``EXISTS metadata WHERE file_id=... AND failed_at IS NULL`` (a metadata failure row carries
    ``failed_at`` set + payload NULL).
  * D-03: analyze backfills, metadata does NOT -- ``metadata.failed_at`` stays all-NULL
    (``report_metadata_failed`` persisted no historical source); the marker records go-forward only.
  * D-05: LOCAL_ANALYZING is ``in_flight(analyze)`` derived from the SAQ in-flight job set in a
    later phase, NOT stored -- a dead local job correctly re-derives as ``not_started`` and re-enqueues.

``files.state`` is byte-unchanged -- this migration NEVER writes it (it is the READ-only backfill source).

CRITICAL: this migration must NEVER reference ``saq_jobs`` (SAQ owns that table via ``init_db`` +
``saq_versions``; an Alembic migration touching it would collide -- 020/031 CRITICAL banner). The
D-05 LOCAL_ANALYZING derivation reads the SAQ in-flight job set in a LATER phase, never here.

D-09: ``downgrade()`` is minimal best-effort DDL reversal only (drops the indexes/table/columns,
restores the 6-member CHECK). The set-based data backfills are NOT reversed (no-op, 016 precedent).

Revision ID: 032
Revises: 031
Create Date: 2026-07-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "032"
down_revision: str | Sequence[str] | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Bare name ``status_enum`` -- the ``ck_%(table_name)s_%(constraint_name)s`` naming convention
# re-applies the ``ck_cloud_job_`` prefix (passing the already-prefixed name double-prefixes it).
_STATUS_ENUM_OLD = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed')"
_STATUS_ENUM_NEW = "status IN ('uploading', 'uploaded', 'submitted', 'running', 'succeeded', 'failed', 'awaiting')"

# Backfill statements -- static string literals only (no interpolation, no model import, no saq_jobs):
# the state values are fixed FileState literals, so there is no injection surface (016 precedent).
_BACKFILL_ANALYZE_FAILED = """
INSERT INTO analysis (id, file_id, failed_at, error_message, created_at, updated_at)
SELECT gen_random_uuid(), f.id, COALESCE(f.updated_at, now()),
       'backfilled from ANALYSIS_FAILED', now(), now()
FROM files f
WHERE f.state = 'analysis_failed'
ON CONFLICT (file_id) DO UPDATE
  SET failed_at = COALESCE(analysis.failed_at, EXCLUDED.failed_at),
      error_message = COALESCE(analysis.error_message, EXCLUDED.error_message)
"""

_BACKFILL_DEDUP = """
INSERT INTO dedup_resolution (id, file_id, canonical_file_id, resolved_at)
SELECT gen_random_uuid(), f.id,
       (SELECT c.id FROM files c
        WHERE c.sha256_hash = f.sha256_hash AND c.state <> 'duplicate_resolved'
        ORDER BY c.id LIMIT 1),
       COALESCE(f.updated_at, now())
FROM files f
WHERE f.state = 'duplicate_resolved'
ON CONFLICT (file_id) DO NOTHING
"""

_BACKFILL_CLOUD_AWAITING = """
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'awaiting'
FROM files f
WHERE f.state = 'awaiting_cloud'
ON CONFLICT (file_id) DO NOTHING
"""

_BACKFILL_CLOUD_PUSHING = """
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'uploading'
FROM files f
WHERE f.state = 'pushing'
ON CONFLICT (file_id) DO NOTHING
"""

_BACKFILL_CLOUD_PUSHED = """
INSERT INTO cloud_job (id, file_id, status)
SELECT gen_random_uuid(), f.id, 'uploaded'
FROM files f
WHERE f.state = 'pushed'
ON CONFLICT (file_id) DO NOTHING
"""


def upgrade() -> None:
    """Add failure markers + dedup sidecar + widened cloud CHECK + partial indexes, then backfill."""
    # (A) Additive nullable failure-marker columns on the 1:1 analysis/metadata output tables (D-01).
    op.add_column("analysis", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("analysis", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("metadata", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("metadata", sa.Column("error_message", sa.Text(), nullable=True))

    # (B) New dedup_resolution 1:1 sidecar (D-07). canonical_file_id is NULLABLE (best-effort pointer).
    op.create_table(
        "dedup_resolution",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dedup_resolution")),
        sa.UniqueConstraint("file_id", name=op.f("uq_dedup_resolution_file_id")),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name=op.f("fk_dedup_resolution_file_id_files")),
        sa.ForeignKeyConstraint(["canonical_file_id"], ["files.id"], name=op.f("fk_dedup_resolution_canonical_file_id_files")),
    )

    # (C) Widen the cloud_job status CHECK to admit 'awaiting' (D-04). Bare name -- see module comment.
    op.drop_constraint("status_enum", "cloud_job", type_="check")
    op.create_check_constraint("status_enum", "cloud_job", _STATUS_ENUM_NEW)

    # (D) Partial indexes mirroring the ORM __table_args__ byte-for-byte (PERF-01 empty-diff contract).
    # Plain transactional builds -- house style, no CONCURRENTLY anywhere in-tree.
    op.create_index("ix_analysis_completed", "analysis", ["file_id"], postgresql_where=sa.text("analysis_completed_at IS NOT NULL"))
    op.create_index("ix_analysis_failed", "analysis", ["file_id"], postgresql_where=sa.text("failed_at IS NOT NULL"))
    op.create_index("ix_metadata_failed", "metadata", ["file_id"], postgresql_where=sa.text("failed_at IS NOT NULL"))
    op.create_index("ix_cloud_job_awaiting", "cloud_job", ["file_id"], postgresql_where=sa.text("status = 'awaiting'"))
    # fingerprint success spelled `= ANY (ARRAY[...])`, NEVER bare IN -- Postgres reserializes IN to
    # = ANY(ARRAY[...]) which would break the empty-autogenerate-diff comparison (RESEARCH Pitfall 1).
    op.create_index("ix_fprint_success", "fingerprint_results", ["file_id"], postgresql_where=sa.text("status = ANY (ARRAY['success','completed'])"))

    # (E) Set-based backfills (READ-ONLY on files.state; never written). analyze = UPSERT; the rest
    # gap-fill sidecar rows only. metadata gets NO backfill (D-03: no historical source).
    op.execute(sa.text(_BACKFILL_ANALYZE_FAILED))
    op.execute(sa.text(_BACKFILL_DEDUP))
    op.execute(sa.text(_BACKFILL_CLOUD_AWAITING))
    op.execute(sa.text(_BACKFILL_CLOUD_PUSHING))
    op.execute(sa.text(_BACKFILL_CLOUD_PUSHED))


def downgrade() -> None:
    """Minimal best-effort DDL reversal (D-09) -- drop indexes/table/columns, restore the 6-member CHECK.

    The set-based data backfills are NOT reversed (no-op, 016 precedent): the migration cannot know
    which marker/sidecar rows pre-existed. Restoring the 6-member CHECK assumes no ``'awaiting'`` rows
    remain (the per-migration test clears them before downgrading).
    """
    op.drop_index("ix_fprint_success", table_name="fingerprint_results")
    op.drop_index("ix_cloud_job_awaiting", table_name="cloud_job")
    op.drop_index("ix_metadata_failed", table_name="metadata")
    op.drop_index("ix_analysis_failed", table_name="analysis")
    op.drop_index("ix_analysis_completed", table_name="analysis")
    op.drop_table("dedup_resolution")
    op.drop_constraint("status_enum", "cloud_job", type_="check")
    op.create_check_constraint("status_enum", "cloud_job", _STATUS_ENUM_OLD)
    op.drop_column("metadata", "error_message")
    op.drop_column("metadata", "failed_at")
    op.drop_column("analysis", "error_message")
    op.drop_column("analysis", "failed_at")
