"""Create the ``analytics_ro`` role and the stable ``analytics_*`` views it may read (phaze-p22p8).

A read-only consumer of phaze's data used to have exactly one way in: the superuser-level application role.
This migration gives it a dedicated role that can ``SELECT`` from a small, documented set of views and nothing else:

* ``analytics_files`` / ``analytics_file_analysis`` -- file identity and the per-file analysis summary;
* ``analytics_analysis_windows`` -- the per-window time series;
* ``analytics_tracklists`` / ``analytics_tracklist_tracks`` -- canonical tracklists and the tracks of their latest version;
* ``analytics_cue_track_offsets`` -- the INTRINSIC per-track timestamps of each media's SELECTED CUE source observation
  (origin: the CUE file; no availability filtering);
* ``analytics_cue_recording_offsets`` -- the TARGET-BOUND qualified offsets: the same tracks, only while the selection is
  current, with origin = the recording. Mirrors ``local_source_import._project_tracks`` / ``get_selected_recording_source``.

The views are a stable interface: their names, column names and column types are a contract with readers outside phaze,
so a migration that touches a column any of them projects MUST update the view in the same change. The column contract
and the stability rule are written down in ``docs/analytics-read-only-access.md``.

The views run with their owner's privileges (the default for a view), so ``analytics_ro`` needs no privilege on any base
table, and has none: PUBLIC holds no table privilege in this schema.

The ROLE is not created here (phaze-euo4a). It is cluster-wide, it carries a secret, and creating it needs ``CREATEROLE``,
which production's app role does not have: the first version of this migration ran ``CREATE ROLE`` and crash-looped the
2026.11.0 deploy. The deployment provisions it, before phaze starts on this release, as ``LOGIN`` with
``default_transaction_read_only = on`` as a second wall behind the missing ``INSERT``/``UPDATE``/``DELETE`` grants
(``docs/analytics-read-only-access.md``, "Provisioning the role"). ``upgrade`` refuses to run without it; ``downgrade``
drops the views and revokes the schema grant, and never drops the role.

Revision ID: 095
Revises: 094
"""

# Every statement below is a literal: no SQL is formatted at run time (Semgrep's formatted-SQL rules, phaze-2al7q.3).
# The role name and the view names are therefore spelled out in each statement; the tuples keep them in one place each.

from collections.abc import Sequence

from alembic import op


revision: str = "095"
down_revision: str | None = "094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "analytics_ro"

# files.file_type values that are media (companion_content.MEDIA_FILE_TYPES at the time of writing) are frozen in the
# recording-offsets view body on purpose: a migration must not change meaning when application code later changes.

# (view name, CREATE VIEW statement), ordered so that drop order (reverse) never leaves a dependency; no view reads another.
VIEWS: tuple[tuple[str, str], ...] = (
    (
        "analytics_files",
        r"""
        CREATE VIEW analytics_files AS
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
    ),
    (
        "analytics_file_analysis",
        r"""
        CREATE VIEW analytics_file_analysis AS
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
    ),
    (
        "analytics_analysis_windows",
        r"""
        CREATE VIEW analytics_analysis_windows AS
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
    ),
    (
        "analytics_tracklists",
        r"""
        CREATE VIEW analytics_tracklists AS
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
    ),
    (
        "analytics_tracklist_tracks",
        r"""
        CREATE VIEW analytics_tracklist_tracks AS
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
    ),
    (
        "analytics_cue_track_offsets",
        r"""
        CREATE VIEW analytics_cue_track_offsets AS
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
              WHERE substring(ev.item FROM '^FILE:(\d+):')::integer = (s.target_mapping ->> 'cue_file_ordinal')::integer
          )
        """,
    ),
    (
        "analytics_cue_recording_offsets",
        r"""
        CREATE VIEW analytics_cue_recording_offsets AS
        SELECT s.media_id AS file_id,
               s.observation_id,
               s.selected_at,
               (s.target_mapping ->> 'cue_file_ordinal')::integer AS cue_file_ordinal,
               (tr.item ->> 'position')::integer AS position,
               tr.item ->> 'artist' AS artist,
               tr.item ->> 'title' AS title,
               tr.item -> 'timestamp' ->> 'original' AS timestamp_original,
               tr.item -> 'timestamp' ->> 'precision' AS timestamp_precision,
               'recording:' || s.media_id::text AS origin,
               (tr.item -> 'timestamp' -> 'offset' ->> 'numerator')::bigint AS offset_numerator,
               (tr.item -> 'timestamp' -> 'offset' ->> 'denominator')::bigint AS offset_denominator,
               ((tr.item -> 'timestamp' -> 'offset' ->> 'numerator')::numeric
                / NULLIF((tr.item -> 'timestamp' -> 'offset' ->> 'denominator')::numeric, 0)) AS offset_seconds
        FROM provider_recording_selections s
        JOIN provider_source_observations o ON o.id = s.observation_id
        JOIN provider_source_objects so ON so.id = o.object_id
        JOIN files m ON m.id = s.media_id
        LEFT JOIN files sf ON sf.id = so.source_file_id
        CROSS JOIN LATERAL (
            SELECT CASE
                       WHEN jsonb_typeof(o.payload -> 'provenance') = 'array' THEN o.payload -> 'provenance'
                       WHEN o.payload ? 'provenance' THEN '[]'::jsonb
                       WHEN jsonb_typeof(o.payload -> 'evidence') = 'array' THEN o.payload -> 'evidence'
                       ELSE '[]'::jsonb
                   END AS items
        ) AS prov
        CROSS JOIN LATERAL jsonb_array_elements(o.payload -> 'tracks') AS tr(item)
        WHERE s.kind = 'tracklist'
          AND o.payload ->> 'source_format' = 'cue'
          AND jsonb_typeof(o.payload -> 'tracks') = 'array'
          AND s.target_mapping ? 'cue_file_ordinal'
          -- the media is present, is media, and is the file the operator reviewed (sha binding)
          AND m.missing_at IS NULL
          AND m.file_type IN ('aac', 'aiff', 'avi', 'flac', 'flv', 'm4a', 'mkv', 'mov', 'mp2', 'mp3', 'mp4', 'ogg', 'opus', 'wav', 'webm', 'wma', 'wmv')
          AND m.sha256_hash = s.target_mapping ->> 'media_sha256'
          -- the source is available (provider_persistence.source_availability == 'current')
          AND o.status IN ('found', 'incomplete', 'ambiguous')
          AND (
              so.channel IS NULL
              OR (
                  so.channel = 'remote'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM provider_source_reads r
                      JOIN provider_source_observations o2 ON o2.id = r.observation_id
                      WHERE r.object_id = so.id
                        AND o2.status = 'absent'
                        AND r.read_seq > (SELECT max(r2.read_seq) FROM provider_source_reads r2 WHERE r2.observation_id = o.id)
                  )
              )
              OR (
                  so.channel = 'companion'
                  AND sf.id IS NOT NULL
                  AND sf.missing_at IS NULL
                  AND sf.companion_ambiguous_at IS NULL
                  AND EXISTS (SELECT 1 FROM file_companions fc WHERE fc.companion_id = sf.id AND fc.media_id = m.id)
                  -- stale only with EXACTLY ONE inventory marker that is not the CUE file's sha; one aggregate
                  -- row always, so no scalar subquery can return two rows (phaze-2al7q.3)
                  AND (
                      SELECT count(*) <> 1 OR bool_and(substring(pe.item FROM 26) = sf.sha256_hash)
                      FROM jsonb_array_elements_text(prov.items) AS pe(item)
                      WHERE left(pe.item, 25) = 'capture:inventory_sha256:'
                  )
                  AND NOT (
                      prov.items ? 'revision:sha256:full'
                      AND o.revision_scope = 'full'
                      AND o.revision ~ '^[0-9a-f]{64}$'
                      AND sf.sha256_hash <> o.revision
                  )
              )
          )
          -- the source file is the media's own agent's, and is the one the operator reviewed
          AND (so.source_file_id IS NULL OR (sf.agent_id = m.agent_id AND sf.sha256_hash = s.target_mapping ->> 'source_sha256'))
          -- a sectioned snapshot projects only the mapped section; the FILE ordinal selects the mapped CUE FILE
          AND (
              jsonb_array_length(CASE WHEN jsonb_typeof(o.payload -> 'sections') = 'array' THEN o.payload -> 'sections' ELSE '[]'::jsonb END) = 0
              OR tr.item ->> 'section_ordinal' = s.target_mapping ->> 'text_section_ordinal'
          )
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(COALESCE(tr.item -> 'evidence', '[]'::jsonb)) AS ev(item)
              WHERE substring(ev.item FROM '^FILE:(\d+):')::integer = (s.target_mapping ->> 'cue_file_ordinal')::integer
          )
          -- only an intrinsically qualified offset is ever re-bound to the recording
          AND tr.item -> 'timestamp' ->> 'offset_usability' = 'qualified'
          AND tr.item -> 'timestamp' ->> 'kind' = 'offset'
          AND tr.item -> 'timestamp' -> 'offset' IS NOT NULL
        """,
    ),
)

GRANTS: tuple[str, ...] = (
    "GRANT USAGE ON SCHEMA public TO analytics_ro",
    "GRANT SELECT ON analytics_files TO analytics_ro",
    "GRANT SELECT ON analytics_file_analysis TO analytics_ro",
    "GRANT SELECT ON analytics_analysis_windows TO analytics_ro",
    "GRANT SELECT ON analytics_tracklists TO analytics_ro",
    "GRANT SELECT ON analytics_tracklist_tracks TO analytics_ro",
    "GRANT SELECT ON analytics_cue_track_offsets TO analytics_ro",
    "GRANT SELECT ON analytics_cue_recording_offsets TO analytics_ro",
)

DROPS: tuple[str, ...] = (
    "DROP VIEW IF EXISTS analytics_cue_recording_offsets",
    "DROP VIEW IF EXISTS analytics_cue_track_offsets",
    "DROP VIEW IF EXISTS analytics_tracklist_tracks",
    "DROP VIEW IF EXISTS analytics_tracklists",
    "DROP VIEW IF EXISTS analytics_analysis_windows",
    "DROP VIEW IF EXISTS analytics_file_analysis",
    "DROP VIEW IF EXISTS analytics_files",
)

# The role is provisioned OUTSIDE phaze, before this migration runs (phaze-euo4a): production's app role has no CREATEROLE,
# and a migration that ran CREATE ROLE crash-looped the 2026.11.0 deploy. No migration creates, alters or drops a role
# or locks a shared catalog (tests/shared/test_migrations_issue_no_role_ddl.py). A missing role fails the upgrade here,
# before any DDL, with the step that fixes it: skipping the grants instead would leave views the role, once created,
# can never read -- nothing re-runs a migration.
REQUIRE_ROLE = """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
            RAISE EXCEPTION USING
                ERRCODE = 'undefined_object',
                MESSAGE = 'role "analytics_ro" does not exist, and phaze migrations do not create roles. Provision it before '
                    'upgrading, as a role that may create roles (the cluster superuser): LOGIN, no superuser, no createdb, no '
                    'createrole, no replication, default_transaction_read_only = on. The statements are in '
                    'docs/analytics-read-only-access.md, "Provisioning the role". Then restart phaze to re-run the upgrade.';
        END IF;
    END
    $$
"""

# Only when the role still exists: an operator may drop it once no database holds a grant to it. Never drops the role.
REVOKE_SCHEMA = """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
            REVOKE USAGE ON SCHEMA public FROM analytics_ro;
        END IF;
    END
    $$
"""


def upgrade() -> None:
    """Require the provisioned role, create the views, and grant the role SELECT on the views only."""
    op.execute(REQUIRE_ROLE)
    for _name, create in VIEWS:
        op.execute(create)
    for grant in GRANTS:
        op.execute(grant)


def downgrade() -> None:
    """Drop the views (their grants go with them) and revoke the schema grant. The role is left in place."""
    for drop in DROPS:
        op.execute(drop)
    op.execute(REVOKE_SCHEMA)
