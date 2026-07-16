"""Single baseline migration: the full phaze schema at revision 039 (Phase 102 flatten).

Collapses the retired 001-039 linear chain into one baseline via Alembic's documented
"Prune Old Migration Files" pattern: this file REUSES revision id ``039`` with
``down_revision = None``, so production -- already stamped ``039`` by the pre-flatten
chain -- treats the next ``upgrade head`` as a no-op, while fresh (CI / test / new)
databases build the entire current schema plus required seed rows from this one file.

The DDL below is OUTPUT-ANCHORED, not metadata-anchored: it is the normalized
``pg_dump --schema-only`` of a database built by the real pre-flatten chain
(``scripts/normalize_schema_dump.py`` strips session SETs / ownership / comments and
the Alembic bookkeeping table). Embedding the dump keeps every non-metadata artifact
the chain accreted -- partial indexes, the 033 XOR/NAND CHECK, generated tsvector
columns + GIN/trgm indexes, the pg_trgm extension -- byte-faithful by construction.
Fidelity was proven at flatten time by an empty normalized-dump diff between the
chain-built and baseline-built schemas (Phase 102 merge gate), and is guarded going
forward by ``tests/integration/test_migrations/test_baseline_schema.py``.

Seed rows (a schema-only baseline would be a broken fresh install without them):
- ``pipeline_stage_control``: one row per stage, ``paused=false``, ``priority=50`` (020's seed).
- ``route_control``: the single ``'global'`` row, ``force_local=false`` (031's seed).
Seeds use bound-param INSERTs -- NO string interpolation of any value into the SQL
(threat T-37-01 / T-71-04, the 012/019/020 discipline).

Revision ID: 039
Revises:
Create Date: 2026-07-16
"""

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "039"
down_revision = None
branch_labels = None
depends_on = None

# Normalized ``pg_dump --schema-only`` of the pre-flatten chain output (see module docstring).
_SCHEMA_DDL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE TABLE public.agents (
    id character varying(64) NOT NULL,
    name character varying(128) NOT NULL,
    token_hash character varying(128),
    scan_roots jsonb DEFAULT '[]'::jsonb NOT NULL,
    last_seen_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_status jsonb,
    kind character varying(16) DEFAULT 'fileserver'::character varying NOT NULL,
    CONSTRAINT ck_agents_ck_agents_id_charset CHECK (((id)::text ~ '^[a-z0-9]+(-[a-z0-9]+)*$'::text)),
    CONSTRAINT ck_agents_kind_enum CHECK (((kind)::text = ANY ((ARRAY['fileserver'::character varying, 'compute'::character varying])::text[])))
);
CREATE TABLE public.analysis (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    bpm double precision,
    musical_key character varying(10),
    mood character varying(50),
    style character varying(50),
    fingerprint text,
    features jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    fine_windows_analyzed integer,
    fine_windows_total integer,
    coarse_windows_analyzed integer,
    coarse_windows_total integer,
    sampled boolean,
    analysis_completed_at timestamp with time zone,
    failed_at timestamp with time zone,
    error_message text,
    CONSTRAINT ck_analysis_analysis_completed_xor_failed CHECK ((NOT ((analysis_completed_at IS NOT NULL) AND (failed_at IS NOT NULL))))
);
CREATE TABLE public.analysis_window (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    tier character varying NOT NULL,
    window_index integer NOT NULL,
    start_sec double precision NOT NULL,
    end_sec double precision NOT NULL,
    bpm double precision,
    musical_key character varying(10),
    mood character varying(50),
    style character varying(50),
    danceability double precision,
    features jsonb,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.cloud_job (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    s3_key character varying(255),
    status character varying(16) NOT NULL,
    upload_id character varying(255),
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    kueue_workload character varying(255),
    attempts integer DEFAULT 0 NOT NULL,
    inadmissible boolean DEFAULT false NOT NULL,
    cloud_phase character varying(20),
    backend_id character varying(255),
    staging_bucket character varying(255),
    CONSTRAINT ck_cloud_job_cloud_phase_enum CHECK (((cloud_phase)::text = ANY ((ARRAY['queued_behind_quota'::character varying, 'admitted'::character varying, 'running'::character varying, 'finished'::character varying])::text[]))),
    CONSTRAINT ck_cloud_job_status_enum CHECK (((status)::text = ANY ((ARRAY['uploading'::character varying, 'uploaded'::character varying, 'submitted'::character varying, 'running'::character varying, 'succeeded'::character varying, 'failed'::character varying, 'awaiting'::character varying])::text[])))
);
CREATE TABLE public.dedup_resolution (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    canonical_file_id uuid,
    resolved_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.discogs_links (
    id uuid NOT NULL,
    track_id uuid NOT NULL,
    discogs_release_id character varying(50) NOT NULL,
    discogs_artist text,
    discogs_title text,
    discogs_label text,
    discogs_year integer,
    confidence double precision NOT NULL,
    status character varying(20) DEFAULT 'candidate'::character varying NOT NULL,
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);
CREATE TABLE public.execution_log (
    id uuid NOT NULL,
    proposal_id uuid NOT NULL,
    operation character varying(20) NOT NULL,
    source_path text NOT NULL,
    destination_path text NOT NULL,
    sha256_verified boolean NOT NULL,
    status character varying(20) NOT NULL,
    error_message text,
    executed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.file_companions (
    id uuid NOT NULL,
    companion_id uuid NOT NULL,
    media_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.files (
    id uuid NOT NULL,
    sha256_hash character varying(64) NOT NULL,
    original_path text NOT NULL,
    original_filename text NOT NULL,
    current_path text NOT NULL,
    file_type character varying(10) NOT NULL,
    file_size bigint NOT NULL,
    batch_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(original_filename, ''::text))) STORED,
    agent_id character varying(64) NOT NULL
);
CREATE TABLE public.files_state_archive (
    file_id uuid NOT NULL,
    state character varying(30) NOT NULL,
    archived_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.fingerprint_results (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    engine character varying(30) NOT NULL,
    status character varying(20) NOT NULL,
    error_message text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.metadata (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    artist text,
    title text,
    album text,
    year integer,
    genre text,
    raw_tags jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    track_number integer,
    duration double precision,
    bitrate integer,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, ((((((COALESCE(artist, ''::text) || ' '::text) || COALESCE(title, ''::text)) || ' '::text) || COALESCE(album, ''::text)) || ' '::text) || COALESCE(genre, ''::text)))) STORED,
    failed_at timestamp with time zone,
    error_message text
);
CREATE TABLE public.pipeline_stage_control (
    stage character varying(32) NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    priority smallint DEFAULT 50 NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_pipeline_stage_control_priority_range CHECK (((priority >= 0) AND (priority <= 100)))
);
CREATE TABLE public.proposals (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    proposed_filename text NOT NULL,
    proposed_path text,
    confidence double precision,
    status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    context_used jsonb,
    reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.route_control (
    id character varying(32) NOT NULL,
    force_local boolean DEFAULT false NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.scan_batches (
    id uuid NOT NULL,
    scan_path text NOT NULL,
    status character varying(20) DEFAULT 'running'::character varying NOT NULL,
    total_files integer DEFAULT 0 NOT NULL,
    processed_files integer DEFAULT 0 NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    agent_id character varying(64) NOT NULL,
    completed_at timestamp with time zone,
    last_progress_at timestamp with time zone
);
CREATE TABLE public.scheduling_ledger (
    key character varying(255) NOT NULL,
    function character varying(64) NOT NULL,
    routing character varying(16) NOT NULL,
    payload jsonb NOT NULL,
    enqueued_at timestamp without time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    timeout integer,
    retries integer
);
CREATE TABLE public.stage_skip (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    stage character varying NOT NULL,
    reason text NOT NULL,
    skipped_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_stage_skip_enrich_only CHECK (((stage)::text = ANY ((ARRAY['metadata'::character varying, 'analyze'::character varying, 'fingerprint'::character varying])::text[])))
);
CREATE TABLE public.tag_write_log (
    id uuid NOT NULL,
    file_id uuid NOT NULL,
    before_tags jsonb NOT NULL,
    after_tags jsonb NOT NULL,
    source character varying(30) NOT NULL,
    status character varying(20) NOT NULL,
    discrepancies jsonb,
    error_message text,
    written_at timestamp without time zone DEFAULT now(),
    created_at timestamp without time zone DEFAULT now(),
    updated_at timestamp without time zone DEFAULT now()
);
CREATE TABLE public.tracklist_tracks (
    id uuid NOT NULL,
    version_id uuid NOT NULL,
    "position" integer NOT NULL,
    artist text,
    title text,
    label text,
    "timestamp" character varying(20),
    is_mashup boolean DEFAULT false NOT NULL,
    remix_info text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    confidence double precision
);
CREATE TABLE public.tracklist_versions (
    id uuid NOT NULL,
    tracklist_id uuid NOT NULL,
    version_number integer NOT NULL,
    scraped_at timestamp without time zone DEFAULT now() NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL
);
CREATE TABLE public.tracklists (
    id uuid NOT NULL,
    external_id character varying(50) NOT NULL,
    source_url text NOT NULL,
    file_id uuid,
    match_confidence integer,
    auto_linked boolean DEFAULT false NOT NULL,
    artist text,
    event text,
    date date,
    latest_version_id uuid,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    updated_at timestamp without time zone DEFAULT now() NOT NULL,
    source character varying(30) DEFAULT '1001tracklists'::character varying NOT NULL,
    status character varying(20) DEFAULT 'approved'::character varying NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, ((COALESCE(artist, ''::text) || ' '::text) || COALESCE(event, ''::text)))) STORED
);
ALTER TABLE ONLY public.files_state_archive
    ADD CONSTRAINT files_state_archive_pkey PRIMARY KEY (file_id);
ALTER TABLE ONLY public.agents
    ADD CONSTRAINT pk_agents PRIMARY KEY (id);
ALTER TABLE ONLY public.analysis
    ADD CONSTRAINT pk_analysis PRIMARY KEY (id);
ALTER TABLE ONLY public.analysis_window
    ADD CONSTRAINT pk_analysis_window PRIMARY KEY (id);
ALTER TABLE ONLY public.cloud_job
    ADD CONSTRAINT pk_cloud_job PRIMARY KEY (id);
ALTER TABLE ONLY public.dedup_resolution
    ADD CONSTRAINT pk_dedup_resolution PRIMARY KEY (id);
ALTER TABLE ONLY public.discogs_links
    ADD CONSTRAINT pk_discogs_links PRIMARY KEY (id);
ALTER TABLE ONLY public.execution_log
    ADD CONSTRAINT pk_execution_log PRIMARY KEY (id);
ALTER TABLE ONLY public.file_companions
    ADD CONSTRAINT pk_file_companions PRIMARY KEY (id);
ALTER TABLE ONLY public.files
    ADD CONSTRAINT pk_files PRIMARY KEY (id);
ALTER TABLE ONLY public.fingerprint_results
    ADD CONSTRAINT pk_fingerprint_results PRIMARY KEY (id);
ALTER TABLE ONLY public.metadata
    ADD CONSTRAINT pk_metadata PRIMARY KEY (id);
ALTER TABLE ONLY public.pipeline_stage_control
    ADD CONSTRAINT pk_pipeline_stage_control PRIMARY KEY (stage);
ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT pk_proposals PRIMARY KEY (id);
ALTER TABLE ONLY public.route_control
    ADD CONSTRAINT pk_route_control PRIMARY KEY (id);
ALTER TABLE ONLY public.scan_batches
    ADD CONSTRAINT pk_scan_batches PRIMARY KEY (id);
ALTER TABLE ONLY public.scheduling_ledger
    ADD CONSTRAINT pk_scheduling_ledger PRIMARY KEY (key);
ALTER TABLE ONLY public.stage_skip
    ADD CONSTRAINT pk_stage_skip PRIMARY KEY (id);
ALTER TABLE ONLY public.tag_write_log
    ADD CONSTRAINT pk_tag_write_log PRIMARY KEY (id);
ALTER TABLE ONLY public.tracklist_tracks
    ADD CONSTRAINT pk_tracklist_tracks PRIMARY KEY (id);
ALTER TABLE ONLY public.tracklist_versions
    ADD CONSTRAINT pk_tracklist_versions PRIMARY KEY (id);
ALTER TABLE ONLY public.tracklists
    ADD CONSTRAINT pk_tracklists PRIMARY KEY (id);
ALTER TABLE ONLY public.analysis
    ADD CONSTRAINT uq_analysis_file_id UNIQUE (file_id);
ALTER TABLE ONLY public.cloud_job
    ADD CONSTRAINT uq_cloud_job_file_id UNIQUE (file_id);
ALTER TABLE ONLY public.dedup_resolution
    ADD CONSTRAINT uq_dedup_resolution_file_id UNIQUE (file_id);
ALTER TABLE ONLY public.file_companions
    ADD CONSTRAINT uq_file_companions_pair UNIQUE (companion_id, media_id);
ALTER TABLE ONLY public.metadata
    ADD CONSTRAINT uq_metadata_file_id UNIQUE (file_id);
ALTER TABLE ONLY public.stage_skip
    ADD CONSTRAINT uq_stage_skip_file_stage UNIQUE (file_id, stage);
ALTER TABLE ONLY public.tracklists
    ADD CONSTRAINT uq_tracklists_external_id UNIQUE (external_id);
CREATE INDEX ix_agents_token_hash_active ON public.agents USING btree (token_hash) WHERE (revoked_at IS NULL);
CREATE INDEX ix_analysis_completed ON public.analysis USING btree (file_id) WHERE (analysis_completed_at IS NOT NULL);
CREATE INDEX ix_analysis_failed ON public.analysis USING btree (file_id) WHERE (failed_at IS NOT NULL);
CREATE INDEX ix_analysis_window_bpm_fine ON public.analysis_window USING btree (bpm) WHERE ((tier)::text = 'fine'::text);
CREATE INDEX ix_analysis_window_dance_coarse ON public.analysis_window USING btree (danceability) WHERE ((tier)::text = 'coarse'::text);
CREATE INDEX ix_analysis_window_file_tier_idx ON public.analysis_window USING btree (file_id, tier, window_index);
CREATE INDEX ix_analysis_window_mood ON public.analysis_window USING btree (mood);
CREATE INDEX ix_analysis_window_style ON public.analysis_window USING btree (style);
CREATE INDEX ix_cloud_job_awaiting ON public.cloud_job USING btree (file_id) WHERE ((status)::text = 'awaiting'::text);
CREATE INDEX ix_discogs_links_discogs_release_id ON public.discogs_links USING btree (discogs_release_id);
CREATE INDEX ix_discogs_links_fts ON public.discogs_links USING gin (to_tsvector('simple'::regconfig, ((COALESCE(discogs_artist, ''::text) || ' '::text) || COALESCE(discogs_title, ''::text))));
CREATE INDEX ix_discogs_links_status ON public.discogs_links USING btree (status);
CREATE INDEX ix_discogs_links_track_id ON public.discogs_links USING btree (track_id);
CREATE INDEX ix_execution_log_proposal_id ON public.execution_log USING btree (proposal_id);
CREATE INDEX ix_execution_log_status ON public.execution_log USING btree (status);
CREATE INDEX ix_file_companions_companion_id ON public.file_companions USING btree (companion_id);
CREATE INDEX ix_file_companions_media_id ON public.file_companions USING btree (media_id);
CREATE INDEX ix_files_filename_trgm ON public.files USING gin (original_filename public.gin_trgm_ops);
CREATE INDEX ix_files_search_vector ON public.files USING gin (search_vector);
CREATE INDEX ix_files_sha256_hash ON public.files USING btree (sha256_hash);
CREATE UNIQUE INDEX ix_fprint_file_engine ON public.fingerprint_results USING btree (file_id, engine);
CREATE INDEX ix_fprint_success ON public.fingerprint_results USING btree (file_id) WHERE ((status)::text = ANY (ARRAY['success'::text, 'completed'::text]));
CREATE INDEX ix_metadata_artist_trgm ON public.metadata USING gin (artist public.gin_trgm_ops);
CREATE INDEX ix_metadata_failed ON public.metadata USING btree (file_id) WHERE (failed_at IS NOT NULL);
CREATE INDEX ix_metadata_search_vector ON public.metadata USING gin (search_vector);
CREATE INDEX ix_proposals_status ON public.proposals USING btree (status);
CREATE INDEX ix_scan_batches_agent_id ON public.scan_batches USING btree (agent_id);
CREATE INDEX ix_scheduling_ledger_function ON public.scheduling_ledger USING btree (function);
CREATE INDEX ix_tag_write_log_file_id ON public.tag_write_log USING btree (file_id);
CREATE INDEX ix_tag_write_log_status ON public.tag_write_log USING btree (status);
CREATE INDEX ix_tracklist_tracks_version_id ON public.tracklist_tracks USING btree (version_id);
CREATE INDEX ix_tracklists_artist_trgm ON public.tracklists USING gin (artist public.gin_trgm_ops);
CREATE UNIQUE INDEX ix_tracklists_external_id ON public.tracklists USING btree (external_id);
CREATE INDEX ix_tracklists_file_id ON public.tracklists USING btree (file_id);
CREATE INDEX ix_tracklists_search_vector ON public.tracklists USING gin (search_vector);
CREATE INDEX ix_tracklists_source ON public.tracklists USING btree (source);
CREATE INDEX ix_tracklists_status ON public.tracklists USING btree (status);
CREATE UNIQUE INDEX uq_files_agent_id_original_path ON public.files USING btree (agent_id, original_path);
CREATE UNIQUE INDEX uq_proposals_file_id_pending ON public.proposals USING btree (file_id) WHERE ((status)::text = 'pending'::text);
CREATE UNIQUE INDEX uq_scan_batches_agent_id_live ON public.scan_batches USING btree (agent_id) WHERE ((status)::text = 'live'::text);
ALTER TABLE ONLY public.analysis
    ADD CONSTRAINT fk_analysis_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.analysis_window
    ADD CONSTRAINT fk_analysis_window_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.cloud_job
    ADD CONSTRAINT fk_cloud_job_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.dedup_resolution
    ADD CONSTRAINT fk_dedup_resolution_canonical_file_id_files FOREIGN KEY (canonical_file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.dedup_resolution
    ADD CONSTRAINT fk_dedup_resolution_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.discogs_links
    ADD CONSTRAINT fk_discogs_links_track_id_tracklist_tracks FOREIGN KEY (track_id) REFERENCES public.tracklist_tracks(id);
ALTER TABLE ONLY public.execution_log
    ADD CONSTRAINT fk_execution_log_proposal_id_proposals FOREIGN KEY (proposal_id) REFERENCES public.proposals(id);
ALTER TABLE ONLY public.file_companions
    ADD CONSTRAINT fk_file_companions_companion_id_files FOREIGN KEY (companion_id) REFERENCES public.files(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.file_companions
    ADD CONSTRAINT fk_file_companions_media_id_files FOREIGN KEY (media_id) REFERENCES public.files(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.files
    ADD CONSTRAINT fk_files_agent_id_agents FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.files
    ADD CONSTRAINT fk_files_batch_id_scan_batches FOREIGN KEY (batch_id) REFERENCES public.scan_batches(id);
ALTER TABLE ONLY public.fingerprint_results
    ADD CONSTRAINT fk_fingerprint_results_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.metadata
    ADD CONSTRAINT fk_metadata_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.proposals
    ADD CONSTRAINT fk_proposals_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.scan_batches
    ADD CONSTRAINT fk_scan_batches_agent_id_agents FOREIGN KEY (agent_id) REFERENCES public.agents(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.stage_skip
    ADD CONSTRAINT fk_stage_skip_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.tag_write_log
    ADD CONSTRAINT fk_tag_write_log_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
ALTER TABLE ONLY public.tracklist_tracks
    ADD CONSTRAINT fk_tracklist_tracks_version_id_tracklist_versions FOREIGN KEY (version_id) REFERENCES public.tracklist_versions(id);
ALTER TABLE ONLY public.tracklist_versions
    ADD CONSTRAINT fk_tracklist_versions_tracklist_id_tracklists FOREIGN KEY (tracklist_id) REFERENCES public.tracklists(id);
ALTER TABLE ONLY public.tracklists
    ADD CONSTRAINT fk_tracklists_file_id_files FOREIGN KEY (file_id) REFERENCES public.files(id);
"""

# Mirrors the retired 020 migration's seed exactly (order preserved for auditability).
_SEED_STAGES = ("metadata", "analyze", "fingerprint")

# Every table the baseline creates, for the mirror-image downgrade.
_TABLES = "agents, analysis, analysis_window, cloud_job, dedup_resolution, discogs_links, execution_log, file_companions, files, files_state_archive, fingerprint_results, metadata, pipeline_stage_control, proposals, route_control, scan_batches, scheduling_ledger, stage_skip, tag_write_log, tracklist_tracks, tracklist_versions, tracklists"


def _ddl_statements() -> list[str]:
    """Split ``_SCHEMA_DDL`` into single statements (asyncpg rejects multi-command strings).

    In ``pg_dump`` output a line ending in ``;`` always terminates a statement (no string
    literal in this schema spans lines or embeds a semicolon), so line-wise accumulation
    is a faithful splitter.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in _SCHEMA_DDL.splitlines():
        if not line.strip():
            continue
        buffer.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(buffer))
            buffer = []
    return statements


def upgrade() -> None:
    """Build the full schema, then seed the two control tables (020 + 031 seeds)."""
    for statement in _ddl_statements():
        op.execute(statement)
    bind = op.get_bind()
    for stage in _SEED_STAGES:
        bind.execute(
            sa.text("INSERT INTO pipeline_stage_control (stage, paused, priority, created_at, updated_at) VALUES (:stage, false, 50, NOW(), NOW())"),
            {"stage": stage},
        )
    bind.execute(
        sa.text("INSERT INTO route_control (id, force_local, created_at, updated_at) VALUES (:id, false, NOW(), NOW())"),
        {"id": "global"},
    )


def downgrade() -> None:
    """Drop everything the baseline created so ``downgrade base`` leaves an empty schema."""
    op.execute(f"DROP TABLE IF EXISTS {_TABLES} CASCADE")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
