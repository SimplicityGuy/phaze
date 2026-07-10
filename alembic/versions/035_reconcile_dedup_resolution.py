"""Bidirectional reconcile of the dedup_resolution marker table against files.state (Phase 84, SIDECAR-02, D-04).

One-shot, DATA-ONLY reconcile. It makes the ``dedup_resolution`` marker table agree with
``files.state`` in BOTH directions so ``marker ≡ state`` holds exactly at the cutover instant. Two
static statements:

  1. ``032``'s ``_BACKFILL_DEDUP`` re-run VERBATIM (``032:84-94``): an
     ``INSERT INTO dedup_resolution .. SELECT .. FROM files WHERE state='duplicate_resolved'
     ON CONFLICT (file_id) DO NOTHING`` -- inserts the MISSING markers.
  2. A NEW orphaned-marker ``DELETE`` (``marker exists AND files.state <> 'duplicate_resolved'``) --
     removes markers whose file is no longer resolved.

Since ``032``, no go-forward writer of ``dedup_resolution`` existed (84-CONTEXT D-01): ``resolve_group``
(``services/dedup.py:266-268``) stamped ``f.state = DUPLICATE_RESOLVED`` and never imported
``DedupResolution``. Every group resolved since ``032`` therefore carries ``state='duplicate_resolved'``
with NO marker, violating the HARD shadow invariant ``state=DUPLICATE_RESOLVED ⇒ dedup marker exists``
(``services/shadow_compare.py:135``, ``soft=False``). Symmetrically, ``undo_resolve`` restored ``state``
while leaving any backfilled marker orphaned; once the reader flips to ``NOT EXISTS(marker)`` an orphaned
marker would hide its file from the dedup UI permanently and unreachably.

``035`` reconciles the EXISTING corpus so the Phase-79 gate is green on the live corpus for the first
time; the go-forward writer + fixed undo (Wave 2, seam (b)) keep NEW resolutions consistent.

**Ordering is load-bearing (D-04): ``035`` must land BEFORE any dedup reader flips to a
marker-existence predicate.** Pre-cutover ``files.state`` is still the authority, so ``035`` reconciles
the derived representation *to* it. The failure mode is safe -- a wrongly-deleted marker merely makes
its file reappear in the dedup UI for re-review, while a wrongly-kept one would hide it forever with no
operator path to fix it (which the delete half is precisely there to prevent).

Contract (D-04):
  * SYNC migration -- plain ``def upgrade()`` / ``op.execute(...)``; only ``env.py`` is async.
  * STATIC, parameter-free SQL -- no interpolation, no f-string, no ``.format``, no model import. The
    ``'duplicate_resolved'`` FileState value is a fixed literal, so there is no injection surface
    (016/032/034 precedent).
  * Touches NO ORM-mapped schema -- the ``dedup_resolution`` table + ``uq_dedup_resolution_file_id``
    already shipped in ``032`` (``032:129-141``). No ``__table_args__`` mirroring is needed and
    ``alembic revision --autogenerate`` stays EMPTY (the per-migration test asserts this via
    ``compare_metadata``).
  * IDEMPOTENT -- ``ON CONFLICT (file_id) DO NOTHING`` + ``uq_dedup_resolution_file_id`` make the insert
    half a no-op on re-run; the delete half is set-based and naturally idempotent.
  * ``files.state`` is byte-unchanged -- ``035`` NEVER writes it (it is the READ-only reconcile source).

``downgrade()`` is a NO-OP (documented choice, D-04 Claude's Discretion). Unlike ``034``'s
documented-lossy ``DELETE``, a ``035`` downgrade has no safe target: the insert half backfilled markers
that are indistinguishable from live go-forward writes, and the delete half removed rows that cannot be
reconstructed (the resolved-state they contradicted is gone). Deleting all markers would destroy live
resolutions; deleting none preserves the corpus. Since ``035`` only reconciles pre-existing data (adds
no schema), a no-op downgrade leaves the marker table in its most-consistent state and never destroys a
live go-forward resolution. This is the safer of the two acceptable choices (034's lossy-DELETE vs
no-op) for a pure reconcile.

CRITICAL: this migration must NEVER reference ``saq_jobs`` (SAQ owns that table -- 020/031/032 banner).

Revision ID: 035
Revises: 034
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "035"
down_revision: str | Sequence[str] | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Statement 1: re-run of 032's _BACKFILL_DEDUP (032:84-94), VERBATIM. Static string literal -- no
# interpolation, no model import: the 'duplicate_resolved' FileState value is a fixed literal, so there
# is no injection surface (016/032/034 precedent). ON CONFLICT (file_id) DO NOTHING +
# uq_dedup_resolution_file_id make the backfill idempotent and non-clobbering of any pre-existing marker
# (D-07 first-writer-wins). canonical_file_id is derived best-effort (ORDER BY c.id LIMIT 1 among
# non-resolved same-sha256 members; NULL if none -- the original human keeper is unrecoverable for
# pre-032 resolutions, RESEARCH Pitfall 4).
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

# Statement 2 (NEW -- orphaned-marker delete, D-04): remove every marker whose file is no longer
# resolved. Static, parameter-free, no saq_jobs. Set-based single-statement DELETE over the indexed FK
# (uq_dedup_resolution_file_id). The safe failure mode (a wrongly-deleted marker makes its file reappear
# for re-review) is why this reconciles the derived representation TO the still-authoritative files.state.
_DELETE_ORPHANED_MARKERS = """
DELETE FROM dedup_resolution dr
USING files f
WHERE dr.file_id = f.id AND f.state <> 'duplicate_resolved'
"""


def upgrade() -> None:
    """Reconcile ``dedup_resolution`` to ``files.state`` in both directions (insert missing, delete orphaned)."""
    op.execute(sa.text(_BACKFILL_DEDUP))
    op.execute(sa.text(_DELETE_ORPHANED_MARKERS))


def downgrade() -> None:
    """No-op (documented choice, D-04).

    ``035`` is a pure data reconcile that adds no schema. A downgrade has no safe target: the inserted
    markers are indistinguishable from live go-forward writes, and the deleted markers cannot be
    reconstructed. Deleting all markers would destroy live resolutions; deleting none preserves the
    corpus. A no-op leaves the marker table in its most-consistent state and never destroys a live
    go-forward resolution (the safer of the two acceptable choices vs 034's lossy-DELETE precedent).
    """
