"""Create the ``analytics_ro`` role and the stable ``analytics_*`` views it may read (phaze-p22p8).

A read-only consumer of phaze's data used to have exactly one way in: the superuser-level application role.
This migration gives it a dedicated role that can ``SELECT`` from a small, documented set of views and nothing else:

* ``analytics_files`` / ``analytics_file_analysis`` -- file identity and the per-file analysis summary;
* ``analytics_analysis_windows`` -- the per-window time series;
* ``analytics_tracklists`` / ``analytics_tracklist_tracks`` -- canonical tracklists and the tracks of their latest version;
* ``analytics_cue_track_offsets`` -- the per-track timestamps of each media's SELECTED CUE source observation.

The views are a stable interface: their names, column names and column types are a contract with readers outside phaze,
so a migration that touches a column any of them projects MUST update the view in the same change. The column contract
and the stability rule are written down in ``docs/analytics-read-only-access.md``.

The views run with their owner's privileges (the default for a view), so ``analytics_ro`` needs no privilege on any base
table, and has none: PUBLIC holds no table privilege in this schema. The role is created ``LOGIN`` with NO password --
a migration is the wrong place for a secret; the operator sets it with ``ALTER ROLE analytics_ro PASSWORD ...`` -- and
``default_transaction_read_only = on`` as a second wall behind the missing ``INSERT``/``UPDATE``/``DELETE`` grants.

A role is cluster-wide, not per-database: ``upgrade`` creates it only if absent, and ``downgrade`` drops it only when no
other database still depends on it.

Revision ID: 095
Revises: 094
"""

# DDL cannot take bind parameters; every interpolated value below is a module constant (ROLE, the VIEWS keys and bodies).
# ruff: noqa: S608

from collections.abc import Sequence

from alembic import op


revision: str = "095"
down_revision: str | None = "094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "analytics_ro"

_CUE_FILE_ORDINAL = r"substring(ev.item FROM '^FILE:(\d+):')"

# Ordered so that drop order (reverse) never leaves a dependency; none of the views reads another view.
VIEWS: dict[str, str] = {
    "analytics_files": """
        SELECT f.id AS file_id,
               f.sha256_hash,
               f.original_path,
               f.original_filename,
               f.current_path,
               f.file_type,
               f.file_size,
               f.missing_at,
               f.created_at
        FROM files f
    """,
    "analytics_file_analysis": """
        SELECT a.file_id,
               a.bpm,
               a.musical_key,
               a.mood,
               a.dominant_style,
               a.style,
               a.fine_windows_analyzed,
               a.fine_windows_total,
               a.coarse_windows_analyzed,
               a.coarse_windows_total,
               a.analysis_completed_at,
               a.failed_at
        FROM analysis a
    """,
    "analytics_analysis_windows": """
        SELECT w.id AS window_id,
               w.file_id,
               w.tier,
               w.window_index,
               w.start_sec,
               w.end_sec,
               w.bpm,
               w.musical_key,
               w.mood,
               w.style,
               w.danceability,
               w.energy,
               w.mood_scores
        FROM analysis_window w
    """,
    "analytics_tracklists": """
        SELECT t.id AS tracklist_id,
               t.file_id,
               t.source,
               t.status,
               t.artist,
               t.event,
               t.date,
               t.match_confidence,
               t.auto_linked,
               t.created_at
        FROM tracklists t
        WHERE t.propagated_from_set_key IS NULL
    """,
    "analytics_tracklist_tracks": """
        SELECT t.id AS tracklist_id,
               v.id AS version_id,
               v.version_number,
               k.position,
               k.artist,
               k.title,
               k.label,
               k.timestamp,
               k.timestamp_evidence,
               k.is_mashup,
               k.remix_info,
               k.confidence
        FROM tracklists t
        JOIN tracklist_versions v ON v.tracklist_id = t.id
        JOIN tracklist_tracks k ON k.version_id = v.id
        WHERE t.propagated_from_set_key IS NULL
          AND v.version_number = (SELECT max(v2.version_number) FROM tracklist_versions v2 WHERE v2.tracklist_id = t.id)
    """,
    "analytics_cue_track_offsets": f"""
        SELECT s.media_id AS file_id,
               s.observation_id,
               s.selected_at,
               (s.target_mapping ->> 'cue_file_ordinal')::integer AS cue_file_ordinal,
               (tr.item ->> 'position')::integer AS position,
               tr.item ->> 'artist' AS artist,
               tr.item ->> 'title' AS title,
               tr.item -> 'timestamp' ->> 'original' AS timestamp_original,
               tr.item -> 'timestamp' ->> 'kind' AS timestamp_kind,
               tr.item -> 'timestamp' ->> 'precision' AS timestamp_precision,
               tr.item -> 'timestamp' ->> 'offset_usability' AS offset_usability,
               (tr.item -> 'timestamp' -> 'offset' ->> 'numerator')::bigint AS offset_numerator,
               (tr.item -> 'timestamp' -> 'offset' ->> 'denominator')::bigint AS offset_denominator,
               ((tr.item -> 'timestamp' -> 'offset' ->> 'numerator')::numeric
                / NULLIF((tr.item -> 'timestamp' -> 'offset' ->> 'denominator')::numeric, 0)) AS offset_seconds
        FROM provider_recording_selections s
        JOIN provider_source_observations o ON o.id = s.observation_id
        CROSS JOIN LATERAL jsonb_array_elements(o.payload -> 'tracks') AS tr(item)
        WHERE s.kind = 'tracklist'
          AND o.payload ->> 'source_format' = 'cue'
          AND jsonb_typeof(o.payload -> 'tracks') = 'array'
          AND s.target_mapping ? 'cue_file_ordinal'
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(COALESCE(tr.item -> 'evidence', '[]'::jsonb)) AS ev(item)
              WHERE {_CUE_FILE_ORDINAL}::integer = (s.target_mapping ->> 'cue_file_ordinal')::integer
          )
    """,
}


def upgrade() -> None:
    """Create the role (if absent), the views, and grant the role SELECT on the views only."""
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
            END IF;
        END
        $$
        """
    )
    op.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
    for name, body in VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {body}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    for name in VIEWS:
        op.execute(f"GRANT SELECT ON {name} TO {ROLE}")


def downgrade() -> None:
    """Drop the views, revoke the role's grants, and drop the role unless another database still uses it."""
    for name in reversed(list(VIEWS)):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                BEGIN
                    DROP ROLE {ROLE};
                EXCEPTION WHEN dependent_objects_still_exist THEN
                    RAISE NOTICE 'role {ROLE} is still used by another database; left in place';
                END;
            END IF;
        END
        $$
        """
    )
