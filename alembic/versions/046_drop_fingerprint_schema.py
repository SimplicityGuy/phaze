"""Drop the persisted fingerprint-results schema after runtime removal (phaze-0jpe.4).

phaze-0jpe.2 (runtime) and phaze-0jpe.3 (infrastructure/sidecars) already merged onto this
branch's base and removed every reader/writer of ``fingerprint_results`` -- `models/fingerprint.py`,
the `agent_fingerprint`/`agent_tracklists` routers, `scan_live_set`, the audfprint/panako sidecars,
and every Track-ID UI surface are already gone from `src/phaze`. This migration lands the schema
half that was deliberately deferred so the code removal never landed a half-applied schema (see
`docs/fingerprint-removal-inventory.md` sec. 4 and `tests/integration/test_migrations/
test_baseline_schema.py`'s PENDING-MIGRATION block on `_FROZEN_AUTOGEN_DRIFT`).

**What this migration drops (checked against ``src/phaze`` first -- nothing else in the schema
exists solely to serve fingerprinting):**

1. ``fingerprint_results`` (table + its PK, both partial/unique indexes ``ix_fprint_file_engine``/
   ``ix_fprint_success``, and its FK to ``files`` -- all drop automatically with the table in
   Postgres, no separate DROP INDEX/CONSTRAINT statements needed).
2. ``stage_skip``'s ``ck_stage_skip_enrich_only`` CHECK, narrowed from
   ``stage IN ('metadata','analyze','fingerprint')`` to ``stage IN ('metadata','analyze')``. Any
   existing ``stage='fingerprint'`` force-skip rows are deleted FIRST (the ALTER would otherwise
   fail against them) -- see the data-decision note below. The ORM twin
   (``src/phaze/models/stage_skip.py``) is narrowed in the same commit as this migration to keep
   the empty-autogenerate-diff contract intact.
3. ``pipeline_stage_control``'s seeded ``stage='fingerprint'`` row, deleted for operator hygiene --
   no CHECK enumerates this table's `stage` values (unlike `stage_skip`), so leaving the row would
   be harmless but confusing to an operator inspecting the table (inventory sec. 4).

**Explicitly checked and NOT dropped** (the brief's instruction to verify before dropping, not
trust the inventory blindly): the scheduling ledger (`scheduling_ledger`) has no fingerprint-only
columns and is a straight read of `src/phaze/services/stage_status.py` / `enums/stage.py` -- both
already carry zero `Stage.FINGERPRINT`/`FingerprintResult` references post-phaze-0jpe.2, confirmed
by `rg -i fingerprint src/phaze` returning no hits. `Tracklist.source` (free `String(30)`, no CHECK)
is the one column that *could* still hold `'fingerprint'`-sourced historical rows -- deliberately
left alone, see the decision below.

**Explicit decision on historical ``tracklists.source = 'fingerprint'`` rows (inventory sec. 2.6):**
this migration does **not** purge them. `tasks/tracklist.py::refresh_tracklists` already carries the
phaze-p1vy guard that permanently excludes them from re-scrape (they are structurally
un-rescrapeable -- empty `source_url`), so a surviving row is inert: it renders in the CUE-list
ordering (`routers/cue.py`) and nowhere else. Given both fingerprint engines were confirmed silently
dead in production for weeks before removal (phaze-p3hj outage diagnosis, phaze-iq65 false-success
investigation), any surviving rows are historical, harmless, and already permanently excluded from
future writes now that `scan_live_set` is gone -- purging them buys nothing and destroys a real (if
stale) record of what was matched. **Recorded here for phaze-0jpe.6's operator runbook: leaving
these rows in place is the deliberate choice, not a default.**

**Discarded row count:** the dispatching session's brief cites approximately 22,856
``fingerprint_results`` rows in the production database as of 2026-07-28 (attributed to the
phaze-p3hj/phaze-iq65 investigations, which found the overwhelming majority record engine failures
or false-positive matches rather than usable fingerprint data). This migration's own test
environment provisions every test database schema-only from the migration chain (see
``tests/integration/test_migrations/conftest.py``), so it starts with zero rows in every table --
that production figure could not be independently re-counted from this environment and is recorded
here as attributed context from the dispatcher's investigation, not first-hand verification by this
migration's author. Whatever the true count, ``DROP TABLE`` does not special-case it: every row in
the table is discarded, unconditionally, as this bead's acceptance requires.

**Downgrade -- schema-symmetric, data-irreversible (documented, not silently broken):** `downgrade()`
recreates `fingerprint_results` (identical DDL: table, PK, both indexes, FK) and the
`pipeline_stage_control` seed row, and re-widens the `stage_skip` CHECK -- a downgrade-then-upgrade
round-trip leaves the schema byte-identical to before. It cannot and does not attempt to restore the
discarded row DATA: the ~22,856 `fingerprint_results` rows and any deleted `stage_skip`
`stage='fingerprint'` rows are gone the moment `upgrade()` runs, and no migration can reconstruct
data that was never captured elsewhere. This is a deliberate, stated asymmetry -- schema is
reversible, data is not -- rather than a downgrade that silently fails or silently fabricates empty
data as if nothing were lost.

**Offline (`--sql`) mode:** every statement below is a static string literal with no bound
parameters (`op.execute("...")`), so it renders identically online and offline -- there is nothing
here that touches the `MockConnection`-loses-bind-params defect phaze-0r9a found in the 039
baseline's *parameterized loop* seed inserts (`bind.execute(sa.text(...), {...})` in a `for` loop).
This migration's `pipeline_stage_control` re-seed in `downgrade()` is a single hardcoded literal
INSERT (`VALUES ('fingerprint', false, 50, now(), now())`), not a bound parameter, specifically to
avoid that trap. Verified with ``alembic upgrade 045:046 --sql`` and ``alembic downgrade 046:045
--sql`` against this tree -- both emit the literal SQL below with no placeholder loss.

Revision ID: 046
Revises: 045
Create Date: 2026-07-28
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None

# Static string-literal DDL/DML throughout -- no interpolation, no bound parameters (see the
# offline-mode note in the module docstring). Order matters: stage_skip's fingerprint rows must be
# gone before the CHECK is narrowed, or the ALTER aborts against any existing force-skip row.
_DELETE_FINGERPRINT_STAGE_SKIPS = "DELETE FROM public.stage_skip WHERE stage = 'fingerprint'"

_DROP_STAGE_SKIP_CHECK = "ALTER TABLE public.stage_skip DROP CONSTRAINT ck_stage_skip_enrich_only"
_ADD_STAGE_SKIP_CHECK_NARROW = "ALTER TABLE public.stage_skip ADD CONSTRAINT ck_stage_skip_enrich_only CHECK (stage IN ('metadata', 'analyze'))"
_ADD_STAGE_SKIP_CHECK_WIDE = (
    "ALTER TABLE public.stage_skip ADD CONSTRAINT ck_stage_skip_enrich_only CHECK (stage IN ('metadata', 'analyze', 'fingerprint'))"
)

_DELETE_FINGERPRINT_PIPELINE_STAGE_CONTROL = "DELETE FROM public.pipeline_stage_control WHERE stage = 'fingerprint'"
# Mirrors the 039 baseline's original seed values for this row exactly (paused=false, priority=50).
_REINSERT_FINGERPRINT_PIPELINE_STAGE_CONTROL = (
    "INSERT INTO public.pipeline_stage_control (stage, paused, priority, created_at, updated_at) VALUES ('fingerprint', false, 50, now(), now())"
)

_DROP_FINGERPRINT_RESULTS = "DROP TABLE public.fingerprint_results"

# Recreates the baseline's exact DDL (alembic/versions/039_baseline_schema.py:172-180, :331-332,
# :389-390, :431-432) so a downgrade-then-upgrade round-trip is byte-identical on schema (data is
# NOT recoverable -- see the module docstring's downgrade section).
_CREATE_FINGERPRINT_RESULTS = """
CREATE TABLE public.fingerprint_results (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    engine character varying(30) NOT NULL,
    status character varying(20) NOT NULL,
    error_message text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
)
"""
_ADD_FINGERPRINT_RESULTS_PK = "ALTER TABLE ONLY public.fingerprint_results ADD CONSTRAINT pk_fingerprint_results PRIMARY KEY (id)"
_CREATE_IX_FPRINT_FILE_ENGINE = "CREATE UNIQUE INDEX ix_fprint_file_engine ON public.fingerprint_results USING btree (file_id, engine)"
_CREATE_IX_FPRINT_SUCCESS = (
    "CREATE INDEX ix_fprint_success ON public.fingerprint_results USING btree (file_id) "
    "WHERE ((status)::text = ANY (ARRAY['success'::text, 'completed'::text]))"
)
_ADD_FINGERPRINT_RESULTS_FK = "ALTER TABLE ONLY public.fingerprint_results ADD CONSTRAINT fk_fingerprint_results_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id)"


def upgrade() -> None:
    """Drop fingerprint_results and every fingerprint-only schema fragment (see module docstring)."""
    op.execute(_DELETE_FINGERPRINT_STAGE_SKIPS)
    op.execute(_DROP_STAGE_SKIP_CHECK)
    op.execute(_ADD_STAGE_SKIP_CHECK_NARROW)
    op.execute(_DELETE_FINGERPRINT_PIPELINE_STAGE_CONTROL)
    op.execute(_DROP_FINGERPRINT_RESULTS)


def downgrade() -> None:
    """Restore the schema exactly; discarded row DATA is gone and cannot be restored (see docstring)."""
    op.execute(_CREATE_FINGERPRINT_RESULTS)
    op.execute(_ADD_FINGERPRINT_RESULTS_PK)
    op.execute(_CREATE_IX_FPRINT_FILE_ENGINE)
    op.execute(_CREATE_IX_FPRINT_SUCCESS)
    op.execute(_ADD_FINGERPRINT_RESULTS_FK)
    op.execute(_REINSERT_FINGERPRINT_PIPELINE_STAGE_CONTROL)
    op.execute(_DROP_STAGE_SKIP_CHECK)
    op.execute(_ADD_STAGE_SKIP_CHECK_WIDE)
