"""Rename the five double-prefixed CHECK constraints to the names the ORM actually renders (phaze-x8tof).

WHAT WAS WRONG. ``Base.metadata`` (``models/base.py``) carries a naming convention whose ``ck`` entry is
``ck_%(table_name)s_%(constraint_name)s``. That is the ONLY entry in the convention that interpolates
``%(constraint_name)s``, and that token is precisely what makes SQLAlchemy apply the convention to
EXPLICITLY NAMED constraints as well as anonymous ones -- ``uq`` / ``fk`` / ``ix`` / ``pk`` have no such
token, so they only ever fire on an unnamed constraint and can never double. A ``CheckConstraint`` whose
declared name already carries the ``ck_<table>_`` prefix therefore gets it a SECOND time::

    CheckConstraint("chains_spent >= 0", name="ck_cloud_budget_chains_spent_nonneg")
    -> ck_cloud_budget_ck_cloud_budget_chains_spent_nonneg

Alembic is not exempt: ``alembic/operations/schemaobj.py`` builds the throwaway ``MetaData`` behind every
``op.create_table`` / ``op.create_check_constraint`` with ``target_metadata.naming_convention`` (env.py
passes ``Base.metadata``), so a migration that writes a pre-prefixed name doubles it in exactly the same
way. The ORM models have always declared BARE names, so the two sides disagreed the moment a migration
wrote a prefixed one.

WHY NOW. This is not new damage and migration 055 (phaze-2mwyo) did not cause it. ``agents``'s doubled
``ck_agents_ck_agents_id_charset`` is baked into the 039 baseline itself, and ``filename_convention``'s
arrived with migration 053. Both predate 055 by months. What changed is the GATE: alembic 1.19.0 (picked
up in the 2026.8.1 pre-release dependency refresh) added ``autogenerate/compare/check_constraints.py``, a
CHECK-constraint comparator that 1.18.5 simply did not have. ``test_autogenerate_drift_is_frozen`` had
therefore never been able to see check-constraint drift; the upgrade made a long-standing defect visible,
and 055 added three fresh instances of it in the same release.

THE 63-CHARACTER PROOF. ``filename_convention``'s doubled name did not even survive intact --
``ck_filename_convention_ck_filename_convention_counts_non_negative`` is 66 characters, past PostgreSQL's
63-byte identifier limit, so SQLAlchemy truncated and hash-suffixed it to
``ck_filename_convention_ck_filename_convention_counts_no_e237``. That is the reason this is repaired
rather than frozen: the doubling was already colliding with the identifier limit, and two sufficiently
long check-constraint names on one table would truncate into each OTHER.

WHAT THIS MIGRATION DOES. Renames the five constraints in place. ``ALTER TABLE ... RENAME CONSTRAINT``
is a catalog-only operation -- no table rewrite, no validation scan, no lock beyond the brief
ACCESS EXCLUSIVE the catalog update takes -- so this is safe on the production ``agents`` /
``filename_convention`` tables at any size.

Every rename is GUARDED on the old name still being present, which makes the migration idempotent and
makes it a no-op wherever the name is already correct. That guard is load-bearing for ``cloud_budget``:
055 was amended in the same change to declare bare names, so a database migrated from empty AFTER that
amendment already has the correct three names and must not error here -- while the release branch's
CI/dev databases, which ran the un-amended 055, still need the rename.

``filename_convention``'s old name is matched by PREFIX rather than spelled out, because the ``_e237``
suffix is a SQLAlchemy-version-dependent truncation hash; a database built under a different SQLAlchemy
could carry a different suffix for the same defect.

DOWNGRADE deliberately restores the doubled (and re-truncated) names, so a downgrade/upgrade round-trip
returns the schema byte-identically. It restores a known-defective name on purpose -- reversibility, not
endorsement.

GOING FORWARD the trap is disarmed three ways: models and migrations both declare BARE check-constraint
names, alembic 1.19's comparator now fails ``test_autogenerate_drift_is_frozen`` on any new doubling, and
``test_no_check_constraint_is_double_prefixed`` names the defect directly so the failure is legible.

Revision ID: 056
Revises: 055
Create Date: 2026-08-07
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


# Static string-literal DDL throughout -- no interpolation of Python values, no bound parameters, so this
# renders correctly in offline (`alembic upgrade --sql`) mode too (migration 046's discipline, and the
# invariant test_baseline_seed_inserts_render_bound_params_in_offline_sql_mode guards).
#
# `ALTER TABLE ... RENAME CONSTRAINT` has no IF EXISTS form, hence the DO block: each rename is gated on
# the old name actually being present, so re-running is a no-op rather than an error.
_UPGRADE_RENAMES = """
DO $$
DECLARE
    v_old text;
BEGIN
    -- agents: doubled since the 039 baseline (alembic/versions/039_baseline_schema.py).
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.agents'::regclass
           AND conname = 'ck_agents_ck_agents_id_charset'
    ) THEN
        ALTER TABLE public.agents
            RENAME CONSTRAINT ck_agents_ck_agents_id_charset TO ck_agents_id_charset;
    END IF;

    -- cloud_budget: doubled by the as-merged migration 055, which has since been amended to bare names.
    -- Fires only on a database that ran the pre-amendment 055.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cloud_budget'::regclass
           AND conname = 'ck_cloud_budget_ck_cloud_budget_chains_spent_nonneg'
    ) THEN
        ALTER TABLE public.cloud_budget
            RENAME CONSTRAINT ck_cloud_budget_ck_cloud_budget_chains_spent_nonneg
                           TO ck_cloud_budget_chains_spent_nonneg;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cloud_budget'::regclass
           AND conname = 'ck_cloud_budget_ck_cloud_budget_attempts_spent_nonneg'
    ) THEN
        ALTER TABLE public.cloud_budget
            RENAME CONSTRAINT ck_cloud_budget_ck_cloud_budget_attempts_spent_nonneg
                           TO ck_cloud_budget_attempts_spent_nonneg;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cloud_budget'::regclass
           AND conname = 'ck_cloud_budget_ck_cloud_budget_node_loss_spent_nonneg'
    ) THEN
        ALTER TABLE public.cloud_budget
            RENAME CONSTRAINT ck_cloud_budget_ck_cloud_budget_node_loss_spent_nonneg
                           TO ck_cloud_budget_node_loss_spent_nonneg;
    END IF;

    -- filename_convention: doubled by migration 053, then TRUNCATED at 63 bytes with a
    -- SQLAlchemy-generated hash suffix. Matched by prefix because that suffix is not stable across
    -- SQLAlchemy versions (see the module docstring).
    SELECT conname INTO v_old
      FROM pg_constraint
     WHERE conrelid = 'public.filename_convention'::regclass
       AND contype = 'c'
       AND conname LIKE 'ck\\_filename\\_convention\\_ck\\_filename\\_convention\\_counts%'
     LIMIT 1;
    IF v_old IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE public.filename_convention RENAME CONSTRAINT %I TO %I',
            v_old,
            'ck_filename_convention_counts_non_negative'
        );
    END IF;
END
$$
"""

# Restores the pre-056 (defective) names so downgrade/upgrade round-trips cleanly. The
# filename_convention name is the truncation SQLAlchemy produces today; see the module docstring.
_DOWNGRADE_RENAMES = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.agents'::regclass
           AND conname = 'ck_agents_id_charset'
    ) THEN
        ALTER TABLE public.agents
            RENAME CONSTRAINT ck_agents_id_charset TO ck_agents_ck_agents_id_charset;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cloud_budget'::regclass
           AND conname = 'ck_cloud_budget_chains_spent_nonneg'
    ) THEN
        ALTER TABLE public.cloud_budget
            RENAME CONSTRAINT ck_cloud_budget_chains_spent_nonneg
                           TO ck_cloud_budget_ck_cloud_budget_chains_spent_nonneg;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cloud_budget'::regclass
           AND conname = 'ck_cloud_budget_attempts_spent_nonneg'
    ) THEN
        ALTER TABLE public.cloud_budget
            RENAME CONSTRAINT ck_cloud_budget_attempts_spent_nonneg
                           TO ck_cloud_budget_ck_cloud_budget_attempts_spent_nonneg;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.cloud_budget'::regclass
           AND conname = 'ck_cloud_budget_node_loss_spent_nonneg'
    ) THEN
        ALTER TABLE public.cloud_budget
            RENAME CONSTRAINT ck_cloud_budget_node_loss_spent_nonneg
                           TO ck_cloud_budget_ck_cloud_budget_node_loss_spent_nonneg;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.filename_convention'::regclass
           AND conname = 'ck_filename_convention_counts_non_negative'
    ) THEN
        ALTER TABLE public.filename_convention
            RENAME CONSTRAINT ck_filename_convention_counts_non_negative
                           TO ck_filename_convention_ck_filename_convention_counts_no_e237;
    END IF;
END
$$
"""


def upgrade() -> None:
    """Rename every double-prefixed CHECK constraint to the single-prefixed name the ORM renders."""
    op.execute(_UPGRADE_RENAMES)


def downgrade() -> None:
    """Restore the doubled names so the round-trip is schema-identical (see the module docstring)."""
    op.execute(_DOWNGRADE_RENAMES)
