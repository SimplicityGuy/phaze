"""Drop the dead ``files.state`` column + ``ix_files_state`` index -- guarded, archived, reversible (Phase 90, MIG-04, PR-C).

The IRREVERSIBLE finale of the Parallel-Enrich-DAG state retirement. PR-A (90-01) made every live
``files.state`` READER derive from the output tables (markers / ``cloud_job`` / ledger); PR-B (90-02)
removed every WRITER. ``files.state`` is now written by nothing and read by nothing in ``src/phaze`` --
a fully dead column. This migration retires it.

Ordered single-transaction ``upgrade()`` body (``alembic/env.py`` runs one revision inside ONE outer
``begin_transaction`` -- NO ``transaction_per_migration``). Any ``raise`` rolls the WHOLE migration back,
so the DROP is never reached on a bad corpus:

  1. GUARD FIRST (D-06/D-07) -- two inline COUNTs against the durable output tables:
     * MID-FLIGHT: ``files.state IN ('pushing','uploading')`` OR a non-terminal ``cloud_job``
       (``status IN ('uploading','submitted','running')``). A mid-flight row means bytes are moving to
       cloud; dropping the cursor mid-transit is unsafe. Deploy under ``--profile drain`` so this is 0.
     * SHADOW-COMPARE: an anti-join summing the HARD invariants TRANSCRIBED (never imported) from
       ``services.shadow_compare.INVARIANTS`` -- ``state = X AND NOT <derived-condition>`` for the
       durable states (metadata_extracted / analyzed / analysis_failed / proposal_generated /
       awaiting_cloud / pushing / pushed / duplicate_resolved / approved / rejected / executed / failed
       / moved->executed / unchanged->failed). The SOFT allowlist (``fingerprinted``,
       ``local_analyzing``) and the vacuous ``discovered`` are NEVER gated (shadow_compare D-06).
     Either COUNT > 0 -> ``raise RuntimeError``. Because every COUNT is 0 on an empty DB, a fresh DB
     passes cleanly (D-06 -- no Phase-89-038 CR-02 fresh-DB-abort footgun). An operator escape hatch
     ``-x force=1`` skips the guard (rehearsed force-drop only).

  2. ARCHIVE (Pattern 2, D-10) -- ``CREATE TABLE files_state_archive(file_id PK, state, archived_at)``
     then ``INSERT ... SELECT id, state FROM files``: a verbatim forensic snapshot so ``downgrade()``
     restores losslessly. It carries file_id UUIDs + the state scalar ONLY -- never a path/filename
     (T-90-pii).

  3. DELTA top-up -- re-runs migration 032's marker backfills idempotently
     (``INSERT ... ON CONFLICT DO NOTHING`` / analyze-failed ``DO UPDATE``). On a guard-green corpus the
     guard SUBSUMES the delta (every state-X file already carries its marker), so these find NOTHING;
     under ``-x force`` they top-up the three 032-derivable markers before the drop.

  4. DROP under a lock-timeout retry wrapper (Pattern 1) -- a per-attempt ``begin_nested()`` SAVEPOINT
     scopes ``SET LOCAL lock_timeout`` so the ACCESS EXCLUSIVE lock on ``files`` ABORTS-AND-RETRIES
     instead of queueing behind the 5s ``/pipeline/stats`` poll (a failed attempt rolls back the
     savepoint ALONE, leaving the outer txn usable). Drops ``ix_files_state`` then ``files.state``.

``downgrade()`` (Pattern 4, D-10): recreate the column (temp ``server_default='discovered'`` to fill
existing rows) + ``ix_files_state``, RESTORE VERBATIM from ``files_state_archive``
(``UPDATE files f SET state = a.state FROM files_state_archive a WHERE a.file_id = f.id`` -- lossless
primary), then apply the D-04/D-05 derived furthest-along FALLBACK for rows absent from the archive
(created after 039), drop the temp default, and DROP the consumed archive table. Lossy fallback cases
(documented, NOT round-tripped): the transient LOCAL_ANALYZING / PUSHING / AWAITING_CLOUD cursors and a
rollback-FINGERPRINTED collapse to their nearest durable derived stage; MOVED/UNCHANGED reconstruct as
executed/failed. The round-trip test asserts only the DURABLE set (all archived, so all verbatim).

Contract (038 discipline):
  * SYNC migration -- plain ``def upgrade()`` / ``op.get_bind().execute(...)``; only ``env.py`` is async.
  * NO ``phaze.*`` imports -- raw ``sa.text`` only; frozen against future model drift (D-07).
  * NEVER references ``saq_jobs`` or ``scheduling_ledger`` (SAQ/ledger tables -- 020/031/032 banner, D-07).
  * ``files.state`` is ``String(30)`` -- NOT a Postgres enum, so NO ``DROP TYPE``; the drop is just
    ``op.drop_index`` + ``op.drop_column``.
  * All operands are fixed FileState-value literals inside ``sa.text`` constants -- no f-string SQL
    surface (T-90-sqli); the only non-state literal is the fixed lock_timeout constant.

Revision ID: 039
Revises: 038
Create Date: 2026-07-12
"""

from collections.abc import Sequence
import time

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
import structlog

from alembic import context, op


logger = structlog.get_logger(__name__)

# revision identifiers, used by Alembic.
revision: str = "039"
down_revision: str | Sequence[str] | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------------------------------
# GUARD (step 1) -- inline, frozen-in-time SQL. NO phaze imports, NO saq_jobs / scheduling_ledger.
# --------------------------------------------------------------------------------------------------

# MID-FLIGHT: bytes in transit to cloud. ``files.state IN ('pushing','uploading')`` covers the state
# cursor; the cloud_job disjunct covers a live-cloud row that advanced past the backfill (rsync/submit/
# run). 'awaiting' (held, not moving) and 'uploaded'/'succeeded' (landed) are NOT mid-flight.
_COUNT_MID_FLIGHT = (
    "SELECT (SELECT count(*) FROM files WHERE state IN ('pushing', 'uploading')) "
    "     + (SELECT count(*) FROM cloud_job WHERE status IN ('uploading', 'submitted', 'running'))"
)

# SHADOW-COMPARE HARD anti-join: files whose scalar state VIOLATES its derived implication. Transcribed
# 1:1 from services.shadow_compare.INVARIANTS (hard entries only). The soft allowlist (fingerprinted,
# local_analyzing) and the vacuous 'discovered' are deliberately ABSENT -- they are never gated.
_COUNT_SHADOW_VIOLATIONS = """
SELECT count(*) FROM files f WHERE
     (f.state = 'metadata_extracted' AND NOT EXISTS (SELECT 1 FROM metadata m WHERE m.file_id = f.id AND m.failed_at IS NULL))
  OR (f.state = 'analyzed'           AND NOT EXISTS (SELECT 1 FROM analysis a WHERE a.file_id = f.id AND a.analysis_completed_at IS NOT NULL))
  OR (f.state = 'analysis_failed'    AND NOT EXISTS (SELECT 1 FROM analysis a WHERE a.file_id = f.id AND a.failed_at IS NOT NULL))
  OR (f.state = 'proposal_generated' AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id))
  OR (f.state = 'awaiting_cloud'     AND NOT EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id = f.id AND c.status = 'awaiting'))
  OR (f.state = 'pushing'            AND NOT EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id = f.id))
  OR (f.state = 'pushed'             AND NOT EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id = f.id))
  OR (f.state = 'duplicate_resolved' AND NOT EXISTS (SELECT 1 FROM dedup_resolution d WHERE d.file_id = f.id))
  OR (f.state = 'approved'           AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'approved'))
  OR (f.state = 'rejected'           AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'rejected'))
  OR (f.state = 'executed'           AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'executed'))
  OR (f.state = 'failed'             AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'failed'))
  OR (f.state = 'moved'              AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'executed'))
  OR (f.state = 'unchanged'          AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'failed'))
"""


# --------------------------------------------------------------------------------------------------
# ARCHIVE (step 2, D-10) -- verbatim forensic snapshot; file_id + state ONLY (no PII, T-90-pii).
# --------------------------------------------------------------------------------------------------
_CREATE_ARCHIVE = (
    "CREATE TABLE files_state_archive ( file_id uuid PRIMARY KEY, state varchar(30) NOT NULL, archived_at timestamptz NOT NULL DEFAULT now())"
)
_FILL_ARCHIVE = "INSERT INTO files_state_archive (file_id, state) SELECT id, state FROM files"


# --------------------------------------------------------------------------------------------------
# DELTA top-up (step 3) -- migration 032's marker backfills, re-run idempotently. Guard-green => no-op.
# Verbatim shape from 032 (analyze-failed UPSERT; dedup / cloud gap-fill ON CONFLICT DO NOTHING).
# --------------------------------------------------------------------------------------------------
_DELTA_ANALYZE_FAILED = """
INSERT INTO analysis (id, file_id, failed_at, error_message, created_at, updated_at)
SELECT gen_random_uuid(), f.id, COALESCE(f.updated_at, now()),
       'backfilled from ANALYSIS_FAILED', now(), now()
FROM files f
WHERE f.state = 'analysis_failed'
ON CONFLICT (file_id) DO UPDATE
  SET failed_at = COALESCE(analysis.failed_at, EXCLUDED.failed_at),
      error_message = COALESCE(analysis.error_message, EXCLUDED.error_message)
"""
_DELTA_DEDUP = """
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
_DELTA_CLOUD_AWAITING = "INSERT INTO cloud_job (id, file_id, status) SELECT gen_random_uuid(), f.id, 'awaiting' FROM files f WHERE f.state = 'awaiting_cloud' ON CONFLICT (file_id) DO NOTHING"
_DELTA_CLOUD_PUSHING = "INSERT INTO cloud_job (id, file_id, status) SELECT gen_random_uuid(), f.id, 'uploading' FROM files f WHERE f.state = 'pushing' ON CONFLICT (file_id) DO NOTHING"
_DELTA_CLOUD_PUSHED = "INSERT INTO cloud_job (id, file_id, status) SELECT gen_random_uuid(), f.id, 'uploaded' FROM files f WHERE f.state = 'pushed' ON CONFLICT (file_id) DO NOTHING"


# --------------------------------------------------------------------------------------------------
# DROP wrapper (step 4, Pattern 1). The ACCESS EXCLUSIVE lock aborts-and-retries under lock_timeout so
# it never queues behind the hot 5s /pipeline/stats poll. Fixed constants -- no interpolation surface.
# --------------------------------------------------------------------------------------------------
_SET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '2s'"  # plain literal -- never an f-string SQL surface (T-90-sqli)
_MAX_ATTEMPTS = 5
_LOCK_NOT_AVAILABLE = "55P03"  # Postgres lock_not_available SQLSTATE (asyncpg LockNotAvailableError)


# --------------------------------------------------------------------------------------------------
# DOWNGRADE (D-10). Verbatim archive restore is primary; derived furthest-along CASE is the fallback
# for rows created AFTER 039 (absent from the archive). Lossy fallback cases documented in the header.
# --------------------------------------------------------------------------------------------------
_RESTORE_FROM_ARCHIVE = "UPDATE files f SET state = a.state FROM files_state_archive a WHERE a.file_id = f.id"
_DERIVED_FALLBACK = """
UPDATE files f SET state = CASE
    WHEN EXISTS (SELECT 1 FROM dedup_resolution d WHERE d.file_id = f.id) THEN 'duplicate_resolved'
    WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'executed') THEN 'executed'
    WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'rejected') THEN 'rejected'
    WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'approved') THEN 'approved'
    WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id AND p.status = 'failed') THEN 'failed'
    WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id = f.id) THEN 'proposal_generated'
    WHEN EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id = f.id AND c.status = 'awaiting') THEN 'awaiting_cloud'
    WHEN EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id = f.id) THEN 'pushed'
    WHEN EXISTS (SELECT 1 FROM analysis a WHERE a.file_id = f.id AND a.analysis_completed_at IS NOT NULL) THEN 'analyzed'
    WHEN EXISTS (SELECT 1 FROM analysis a WHERE a.file_id = f.id AND a.failed_at IS NOT NULL) THEN 'analysis_failed'
    WHEN EXISTS (SELECT 1 FROM metadata m WHERE m.file_id = f.id AND m.failed_at IS NULL) THEN 'metadata_extracted'
    ELSE 'discovered'
END
WHERE NOT EXISTS (SELECT 1 FROM files_state_archive a WHERE a.file_id = f.id)
"""


def _guard(bind: sa.engine.Connection) -> None:
    """Abort (raise) if the corpus is mid-flight or shadow-compare-inconsistent (D-06/D-07).

    Skipped only when the operator passes ``-x force=1`` (a rehearsed force-drop). Both COUNTs are 0 on
    an empty DB, so a fresh DB passes cleanly (no CR-02 fresh-DB abort).
    """
    if context.get_x_argument(as_dictionary=True).get("force"):
        logger.warning("migration_039_guard_skipped_via_x_force")
        return
    mid_flight = bind.execute(sa.text(_COUNT_MID_FLIGHT)).scalar_one()
    if mid_flight != 0:
        raise RuntimeError(
            f"039 aborted: {mid_flight} mid-flight row(s) (files.state in pushing/uploading OR a non-terminal cloud_job). "
            "Deploy under --profile drain so no bytes are in transit, or re-run with -x force=1 after rehearsal."
        )
    violations = bind.execute(sa.text(_COUNT_SHADOW_VIOLATIONS)).scalar_one()
    if violations != 0:
        raise RuntimeError(
            f"039 aborted: {violations} row(s) violate a HARD shadow-compare invariant (a scalar state whose derived "
            "source is missing). Run shadow-compare green on the drained corpus first, or re-run with -x force=1."
        )


def _drop_state_ddl(bind: sa.engine.Connection) -> None:
    """Drop ix_files_state + files.state under a per-attempt SAVEPOINT + SET LOCAL lock_timeout (Pattern 1).

    env.py runs the migration in ONE outer transaction, so a per-attempt ``begin_nested`` SAVEPOINT is
    the correct scope: a lock timeout rolls back the savepoint ALONE (undoing its ``SET LOCAL``) and the
    outer txn stays usable for the next attempt.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with bind.begin_nested():
                bind.execute(sa.text(_SET_LOCK_TIMEOUT))
                op.drop_index("ix_files_state", table_name="files")
                op.drop_column("files", "state")
            return
        except OperationalError as err:
            sqlstate = getattr(getattr(err, "orig", None), "sqlstate", None)
            if sqlstate != _LOCK_NOT_AVAILABLE or attempt == _MAX_ATTEMPTS:
                raise
            logger.warning("migration_039_drop_lock_timeout_retry", attempt=attempt, max_attempts=_MAX_ATTEMPTS)
            time.sleep(0.5 * attempt)


def upgrade() -> None:
    """Guard -> archive -> delta top-up -> drop ix_files_state + files.state, all in one transaction (D-06/D-07/D-10)."""
    bind = op.get_bind()
    # (1) GUARD FIRST -- any raise rolls the whole txn back before ANY archive/DDL side effect.
    _guard(bind)
    # (2) ARCHIVE the verbatim state snapshot (D-10 reversibility source).
    bind.execute(sa.text(_CREATE_ARCHIVE))
    bind.execute(sa.text(_FILL_ARCHIVE))
    # (3) DELTA top-up -- idempotent 032 backfills; a no-op on a guard-green corpus (guard subsumes it).
    bind.execute(sa.text(_DELTA_ANALYZE_FAILED))
    bind.execute(sa.text(_DELTA_DEDUP))
    bind.execute(sa.text(_DELTA_CLOUD_AWAITING))
    bind.execute(sa.text(_DELTA_CLOUD_PUSHING))
    bind.execute(sa.text(_DELTA_CLOUD_PUSHED))
    # (4) DROP the dead index + column under the lock-timeout retry wrapper.
    _drop_state_ddl(bind)


def downgrade() -> None:
    """Recreate files.state + ix_files_state and restore VERBATIM from files_state_archive (D-10)."""
    bind = op.get_bind()
    # Recreate the column with a temp server_default so existing rows fill; restore overwrites it.
    op.add_column("files", sa.Column("state", sa.String(30), nullable=False, server_default="discovered"))
    op.create_index("ix_files_state", "files", ["state"])
    # (primary) verbatim restore from the archive -- lossless for every pre-039 row.
    bind.execute(sa.text(_RESTORE_FROM_ARCHIVE))
    # (fallback) derived furthest-along reconstruction for rows created AFTER 039 (absent from archive).
    bind.execute(sa.text(_DERIVED_FALLBACK))
    # Drop the temp default (the original column had only a Python-side default, no server_default).
    op.alter_column("files", "state", server_default=None)
    # The archive is consumed -- drop it so a subsequent re-upgrade recreates it cleanly.
    op.drop_table("files_state_archive")
