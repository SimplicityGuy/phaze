"""Reattribute legacy-application-server-owned rows to a real fileserver, then delete the sentinel (Phase 89, LEGACY-02/03, D-01..D-10).

One-shot, DATA-ONLY migration. It remediates the last runtime state that still holds the
``legacy-application-server`` sentinel string after Plan 89-01 removed the source that produced it: the
Postgres ``agents``/``files``/``scan_batches`` rows. It reattributes every historical legacy-owned
``files`` row and (non-live) ``scan_batches`` row to a designated real ``kind='fileserver'`` agent, then
DELETEs the sentinel ``agents`` row. The ``ondelete=RESTRICT`` FK on both tables is satisfiable ONLY
because reattribution runs first, inside one transaction, gated by a COUNT=0 assertion.

Target selection (D-01/D-02):
  * ``-x reattribute_to=<id>`` override, if supplied, is validated against ``agents`` (exists +
    ``kind='fileserver'`` + not revoked) BEFORE use and passed via ``bindparams`` -- never f-stringed
    into SQL (T-89-02-01).
  * Otherwise AUTO-DETECT the sole non-revoked fileserver via
    ``revoked_at IS NULL AND kind='fileserver'`` (mirrors ``enqueue_router.select_active_agent``). The
    legacy agent is auto-excluded -- migration 012 seeds it ``revoked_at=NOW()``. Exactly one match ->
    use it; zero -> abort; more than one (no override) -> abort with operator guidance (D-01/D-02).

Ordered single-transaction body (D-09 + Pitfall 1 fix). Any ``raise`` (invalid override, 0/>1
fileserver, COUNT!=0) rolls the WHOLE migration back under Alembic's ``transaction_per_migration``, so
the sentinel DELETE is never reached in a bad state (T-89-02-02/03/04):
  1. DELETE the legacy ``status='live'`` watcher batch created by migration 012. It is a zero-value
     vestigial sentinel (``scan_path='<watcher>'``, ``total_files=0``). Reattributing it would create a
     SECOND live batch for the target and violate the ``uq_scan_batches_agent_id_live`` partial unique
     index (``UNIQUE (agent_id) WHERE status='live'``, 012:104-110) -- so it is DELETED, not
     reattributed (D-03 refined, RESEARCH Pitfall 1, T-89-02-05).
  1.5. CR-01 guard: abort with clear operator guidance if the target already owns a ``files`` row at a
     legacy file's ``original_path``. The step-2 UPDATE would otherwise duplicate ``(target, original_path)``
     and violate ``uq_files_agent_id_original_path`` (013 / file.py:99) with an opaque ``IntegrityError``.
     013's composite UQ deliberately allows the same path under different agents, so this is a real state.
  2. UPDATE ``files`` -> target (parameterized on ``:target``).
  3. UPDATE ``scan_batches`` -> target (no live rows remain to collide).
  4. Assert ``COUNT(*)`` of remaining legacy-owned rows across ``files`` U ``scan_batches`` == 0, else
     ``raise`` (rolls back BEFORE the sentinel DELETE) (D-09).
  5. DELETE the sentinel ``agents`` row (RESTRICT FK now satisfiable).

Single ``UPDATE`` per table is fine at ~11,428 rows: an indexed FK re-point takes only a ROW EXCLUSIVE
lock and is sub-second -- no batching / lock_timeout needed (that is Phase 90's DDL concern).

Contract:
  * SYNC migration -- plain ``def upgrade()`` / ``op.get_bind().execute(...)``; only ``env.py`` is async.
  * NO model imports -- raw ``sa.text`` in migration-012 style, immune to future model drift.
  * NO DDL of any kind -- migration 012 added the ``agent_id`` columns nullable + backfilled, so there is
    no ``server_default`` to drop (D-07). ``alembic revision --autogenerate`` against the 038 head is an
    EMPTY schema diff.
  * Does NOT touch ``files.state`` / the ``FileState`` enum -- Phase 90 owns that.

``downgrade()`` raises ``NotImplementedError`` (D-10): once the legacy-owned rows are merged into the
target fileserver, original ownership is unrecoverable, so neither the reattribution nor the sentinel row
can be reconstructed. This DEVIATES from the no-op ``pass`` downgrades in 035/036 (those were pure
same-column backfills; this one destroys the distinguishing ownership).

CRITICAL: this migration must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

Revision ID: 038
Revises: 037
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op


# revision identifiers, used by Alembic.
revision: str = "038"
down_revision: str | Sequence[str] | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Target selection predicates. Auto-detect mirrors ``enqueue_router.select_active_agent(kind='fileserver')``:
# the legacy sentinel is auto-excluded because migration 012 seeds it ``revoked_at=NOW()``. The override
# form validates the operator-supplied id against the SAME liveness+kind predicate (parameterized on
# ``:id`` -- never f-stringed, T-89-02-01).
_SELECT_AUTODETECT_FILESERVER = "SELECT id FROM agents WHERE revoked_at IS NULL AND kind='fileserver'"
_VALIDATE_OVERRIDE = "SELECT id FROM agents WHERE id = :id AND revoked_at IS NULL AND kind='fileserver'"

# Step 1 (Pitfall 1 fix): DELETE the legacy status='live' watcher batch created by migration 012 so the
# bulk UPDATE of scan_batches cannot create a second live row for the target under
# uq_scan_batches_agent_id_live. Static literal id -- no interpolation surface.
_DELETE_LEGACY_LIVE_BATCH = "DELETE FROM scan_batches WHERE agent_id = 'legacy-application-server' AND status = 'live'"

# Step 1.5 (CR-01 guard): the reattribution UPDATE below re-points every legacy-owned ``files`` row to
# ``:target``. ``files`` carries the composite unique index ``uq_files_agent_id_original_path`` on
# ``(agent_id, original_path)`` (013 / models/file.py:99). If the target fileserver ALREADY owns a row at
# the same ``original_path`` as any legacy-owned row, the UPDATE would produce a duplicate
# ``(target, original_path)`` and abort mid-transaction with an opaque ``IntegrityError``. Migration 013's
# composite UQ deliberately allows the same ``original_path`` under different agents, so this is a real
# reachable production state. Detect it FIRST and abort with clear operator guidance (mirroring the
# 0/>1-fileserver aborts and 013's D-16 downgrade guard) instead of a raw IntegrityError. Parameterized on
# ``:target`` -- never f-stringed. LIMIT 5 keeps the error message bounded.
_FILES_PATH_COLLISION = (
    "SELECT l.original_path FROM files l "
    "JOIN files t ON t.original_path = l.original_path AND t.agent_id = :target "
    "WHERE l.agent_id = 'legacy-application-server' "
    "ORDER BY l.original_path LIMIT 5"
)

# Steps 2/3: reattribute the surviving legacy-owned rows to the chosen target. Parameterized on
# ``:target`` (bindparams) -- the target id is NEVER f-stringed into the SQL (T-89-02-01).
_REATTRIBUTE_FILES = "UPDATE files SET agent_id = :target WHERE agent_id = 'legacy-application-server'"
_REATTRIBUTE_SCAN_BATCHES = "UPDATE scan_batches SET agent_id = :target WHERE agent_id = 'legacy-application-server'"

# Step 4 (D-09): COUNT remaining legacy-owned rows across files U scan_batches. Must be 0 before the
# sentinel DELETE, else raise (rolls the whole txn back before the DELETE is reached).
_COUNT_REMAINING = (
    "SELECT (SELECT COUNT(*) FROM files WHERE agent_id = 'legacy-application-server') "
    "     + (SELECT COUNT(*) FROM scan_batches WHERE agent_id = 'legacy-application-server')"
)

# Step 5: delete the sentinel agent row. The RESTRICT FK on files/scan_batches is now satisfiable because
# steps 1-3 removed every referencing row and step 4 proved it.
_DELETE_SENTINEL = "DELETE FROM agents WHERE id = 'legacy-application-server'"


def _resolve_target(bind: sa.engine.Connection) -> str:
    """Resolve the reattribution target: ``-x reattribute_to`` override (validated) or the sole fileserver.

    Raises ``RuntimeError`` on an invalid override, zero fileservers, or (without an override) more than
    one fileserver -- each aborts the migration inside its single transaction (D-01/D-02).
    """
    override = context.get_x_argument(as_dictionary=True).get("reattribute_to")
    if override:
        row = bind.execute(sa.text(_VALIDATE_OVERRIDE).bindparams(id=override)).first()
        if row is None:
            raise RuntimeError(f"reattribute_to={override!r} is not a valid non-revoked fileserver agent; aborting.")
        return override

    rows = bind.execute(sa.text(_SELECT_AUTODETECT_FILESERVER)).all()
    if len(rows) == 0:
        raise RuntimeError("No non-revoked fileserver agent exists; cannot reattribute. Aborting.")
    if len(rows) > 1:
        found = [r[0] for r in rows]
        raise RuntimeError(f"Multiple non-revoked fileserver agents found ({found}); pass -x reattribute_to=<id> to choose one.")
    return str(rows[0][0])


def upgrade() -> None:
    """Reattribute legacy-owned files/scan_batches to the target fileserver, then delete the sentinel (D-01..D-09)."""
    bind = op.get_bind()
    target = _resolve_target(bind)

    # (1) DELETE the legacy live watcher batch (Pitfall 1) BEFORE the bulk scan_batches UPDATE.
    bind.execute(sa.text(_DELETE_LEGACY_LIVE_BATCH))
    # (1.5) CR-01 guard: abort with clear operator guidance if the target already owns a file at a
    # legacy file's original_path -- the reattribution UPDATE would otherwise violate
    # uq_files_agent_id_original_path with an opaque IntegrityError. The raise rolls back the whole txn
    # (the sentinel DELETE is never reached), same all-or-nothing contract as the COUNT!=0 gate (D-09).
    collisions = bind.execute(sa.text(_FILES_PATH_COLLISION).bindparams(target=target)).scalars().all()
    if collisions:
        raise RuntimeError(
            f"Cannot reattribute: target {target!r} already owns files at legacy original_path(s) "
            f"{list(collisions)} (composite UQ uq_files_agent_id_original_path would collide). "
            "Resolve these duplicate paths (delete the redundant legacy or target row) before retrying 038."
        )
    # (2)/(3) reattribute the surviving legacy-owned rows (parameterized on :target).
    bind.execute(sa.text(_REATTRIBUTE_FILES).bindparams(target=target))
    bind.execute(sa.text(_REATTRIBUTE_SCAN_BATCHES).bindparams(target=target))
    # (4) COUNT=0 gate -- any remaining legacy-owned row rolls the whole txn back before the DELETE (D-09).
    remaining = bind.execute(sa.text(_COUNT_REMAINING)).scalar_one()
    if remaining != 0:
        raise RuntimeError(f"Reattribution incomplete: {remaining} legacy-owned rows remain; aborting before sentinel DELETE.")
    # (5) RESTRICT FK now satisfiable -- delete the sentinel agent row.
    bind.execute(sa.text(_DELETE_SENTINEL))


def downgrade() -> None:
    """Irreversible (D-10) -- reattribution merged legacy ownership into the target; it cannot be reconstructed."""
    raise NotImplementedError(
        "038 merged legacy-application-server-owned files/scan_batches into the target fileserver; "
        "original ownership is unrecoverable, so the reattribution and sentinel row cannot be reconstructed."
    )
