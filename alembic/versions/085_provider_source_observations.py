"""Expand provider identity and immutable observations, preserving legacy stored data."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "085"
down_revision: str | None = "084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add bounded provider storage without backfilling or changing reviewed selections."""
    op.execute(
        "\nCREATE TABLE provider_source_objects (\n\tid UUID NOT NULL, \n\tprovider_id VARCHAR(64) NOT NULL, \n\tnative_id TEXT NOT NULL, \n\tnative_digest VARCHAR(64) NOT NULL, \n\tsource_file_id UUID, \n\toriginal_file_id UUID, \n\tchannel VARCHAR(16), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_provider_source_objects PRIMARY KEY (id), \n\tCONSTRAINT ck_provider_source_objects_provider_native_bound CHECK (octet_length(native_id) BETWEEN 1 AND 16384), \n\tCONSTRAINT ck_provider_source_objects_provider_channel CHECK (channel IS NULL OR channel IN ('companion', 'embedded')), \n\tCONSTRAINT fk_provider_source_objects_source_file_id_files FOREIGN KEY(source_file_id) REFERENCES files (id) ON DELETE SET NULL\n)\n\n"
    )
    op.execute("CREATE INDEX ix_provider_objects_bucket ON provider_source_objects (provider_id, native_digest)")
    op.execute(
        "\nCREATE TABLE provider_source_observations (\n\tid UUID NOT NULL, \n\tobject_id UUID NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tcode TEXT NOT NULL, \n\trevision TEXT, \n\trevision_scope VARCHAR(16) NOT NULL, \n\tparser_version VARCHAR(128) NOT NULL, \n\tcontent_digest VARCHAR(64) NOT NULL, \n\tpayload JSONB NOT NULL, \n\tdecoded_text TEXT, \n\tencoding TEXT, \n\ttruncated BOOLEAN NOT NULL, \n\tsource_url TEXT, \n\tretrieved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tparent_id UUID, \n\tconflict_with_id UUID, \n\tCONSTRAINT pk_provider_source_observations PRIMARY KEY (id), \n\tCONSTRAINT ck_provider_source_observations_provider_text_bound CHECK (decoded_text IS NULL OR octet_length(decoded_text) <= 1048576), \n\tCONSTRAINT ck_provider_source_observations_provider_revision_bound CHECK (revision IS NULL OR octet_length(revision) <= 8192), \n\tCONSTRAINT ck_provider_source_observations_provider_revision_scope CHECK (revision_scope IN ('full', 'bounded', 'unknown')), \n\tCONSTRAINT fk_provider_source_observations_object_id_provider_sour_26d0 FOREIGN KEY(object_id) REFERENCES provider_source_objects (id), \n\tCONSTRAINT fk_provider_source_observations_parent_id_provider_sour_6779 FOREIGN KEY(parent_id) REFERENCES provider_source_observations (id), \n\tCONSTRAINT fk_provider_source_observations_conflict_with_id_provid_1f32 FOREIGN KEY(conflict_with_id) REFERENCES provider_source_observations (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_provider_observations_content ON provider_source_observations (object_id, content_digest)")
    op.execute("CREATE INDEX ix_provider_observations_object ON provider_source_observations (object_id)")
    op.execute(
        "\nCREATE TABLE provider_recording_candidates (\n\tid UUID NOT NULL, \n\tmedia_id UUID NOT NULL, \n\tobservation_id UUID NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tevidence JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_provider_recording_candidates PRIMARY KEY (id), \n\tCONSTRAINT uq_provider_candidate_target UNIQUE (media_id, observation_id), \n\tCONSTRAINT fk_provider_recording_candidates_media_id_files FOREIGN KEY(media_id) REFERENCES files (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_provider_recording_candidates_observation_id_provide_0e39 FOREIGN KEY(observation_id) REFERENCES provider_source_observations (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_provider_candidates_media ON provider_recording_candidates (media_id)")
    op.execute(
        "\nCREATE TABLE provider_recording_selections (\n\tmedia_id UUID NOT NULL, \n\tkind VARCHAR(128) NOT NULL, \n\tobservation_id UUID NOT NULL, \n\tactor VARCHAR(128) NOT NULL, \n\tselected_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\ttracklist_id UUID, \n\tversion_id UUID, \n\tCONSTRAINT pk_provider_recording_selections PRIMARY KEY (media_id, kind), \n\tCONSTRAINT fk_provider_recording_selections_media_id_files FOREIGN KEY(media_id) REFERENCES files (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_provider_recording_selections_observation_id_provide_5699 FOREIGN KEY(observation_id) REFERENCES provider_source_observations (id), \n\tCONSTRAINT fk_provider_recording_selections_tracklist_id_tracklists FOREIGN KEY(tracklist_id) REFERENCES tracklists (id) ON DELETE SET NULL, \n\tCONSTRAINT fk_provider_recording_selections_version_id_tracklist_versions FOREIGN KEY(version_id) REFERENCES tracklist_versions (id) ON DELETE SET NULL\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE provider_selection_events (\n\tid UUID NOT NULL, \n\tmedia_id UUID NOT NULL, \n\tkind VARCHAR(128) NOT NULL, \n\tobservation_id UUID NOT NULL, \n\tactor VARCHAR(128) NOT NULL, \n\tselected_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_provider_selection_events PRIMARY KEY (id), \n\tCONSTRAINT fk_provider_selection_events_observation_id_provider_so_acbb FOREIGN KEY(observation_id) REFERENCES provider_source_observations (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_provider_selection_events_media ON provider_selection_events (media_id)")
    op.add_column("tracklists", sa.Column("provider_object_id", sa.UUID(), sa.ForeignKey("provider_source_objects.id"), nullable=True))
    op.add_column(
        "tracklist_versions", sa.Column("source_observation_id", sa.UUID(), sa.ForeignKey("provider_source_observations.id"), nullable=True)
    )
    op.add_column("tracklist_tracks", sa.Column("timestamp_evidence", sa.dialects.postgresql.JSONB(), nullable=True))
    op.drop_index("ix_tracklists_external_id", table_name="tracklists")
    op.create_index(
        "ix_tracklists_external_id",
        "tracklists",
        ["external_id"],
        unique=True,
        postgresql_where=sa.text("propagated_from_set_key IS NULL AND provider_object_id IS NULL"),
    )
    op.create_index(
        "ix_tracklists_provider_object",
        "tracklists",
        ["provider_object_id"],
        unique=True,
        postgresql_where=sa.text("propagated_from_set_key IS NULL"),
    )
    op.execute(
        "CREATE FUNCTION reject_provider_observation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'provider source observations are immutable'; END $$"
    )
    op.execute(
        "CREATE TRIGGER provider_observations_immutable BEFORE UPDATE OR DELETE ON provider_source_observations FOR EACH ROW EXECUTE FUNCTION reject_provider_observation_mutation()"
    )
    # Populate statistics for the new nullable columns before application queries use them.
    op.execute("ANALYZE public.tracklists")
    op.execute("ANALYZE public.tracklist_versions")
    op.execute("ANALYZE public.tracklist_tracks")


def downgrade() -> None:
    """Refuse evidence loss; only an unused expansion can be removed safely."""
    connection = op.get_bind()
    used = connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM provider_source_objects) OR EXISTS (SELECT 1 FROM tracklist_tracks WHERE timestamp_evidence IS NOT NULL)"
        )
    )
    if used:
        raise RuntimeError("Provider storage contains evidence: lossy downgrade refused; use forward recovery")
    op.drop_table("provider_selection_events")
    op.drop_table("provider_recording_selections")
    op.drop_table("provider_recording_candidates")
    op.drop_column("tracklist_tracks", "timestamp_evidence")
    op.drop_column("tracklist_versions", "source_observation_id")
    op.drop_index("ix_tracklists_provider_object", table_name="tracklists")
    op.drop_index("ix_tracklists_external_id", table_name="tracklists")
    op.drop_column("tracklists", "provider_object_id")
    op.create_index(
        "ix_tracklists_external_id", "tracklists", ["external_id"], unique=True, postgresql_where=sa.text("propagated_from_set_key IS NULL")
    )
    op.drop_table("provider_source_observations")
    op.execute("DROP FUNCTION reject_provider_observation_mutation()")
    op.drop_table("provider_source_objects")
