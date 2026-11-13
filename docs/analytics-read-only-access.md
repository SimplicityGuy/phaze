# Read-only analytics access

A read-only consumer of phaze's data connects as the dedicated Postgres role **`analytics_ro`**, never as the
application role. The role can `SELECT` from eight `analytics_*` views and from nothing else: no base table, no write, no DDL.
Migration `095_analytics_ro_role.py` creates the first seven views and grants them to the role, and
`096_analytics_agent_path_prefixes.py` adds the eighth; `tests/integration/test_migrations/test_095_analytics_ro_role.py`
connects as the role and proves each of those claims.

**phaze does not create the role.** It is provisioned outside phaze, by the deployment, **before phaze starts on a release
at or past migration 095** (see [Provisioning the role](#provisioning-the-role)). phaze's migrations only grant to it.

## What the role is

| Property | Value |
|---|---|
| Name | `analytics_ro` |
| Attributes | `LOGIN`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`, `NOINHERIT` |
| Grants | `USAGE` on schema `public`; `SELECT` on the eight views below. Nothing on any base table |
| Role setting | `default_transaction_read_only = on`, a second wall behind the missing write grants |
| Password | Set by the deployment from its secret store. Never in a migration, and never in this repository |
| Created by | The deployment, as a role that may create roles. **Never by a phaze migration** |

The views run with their owner's privileges (the Postgres default for a view), which is why the role needs no base-table
privilege: it can see exactly the columns a view projects and no others. Columns that are large or internal (`features`
JSONB on `analysis` and `analysis_window`, `error_message`, source URLs, review and queue state) are deliberately not projected.

The views are owned by the application role (it creates them), so it may grant on them without `CREATEROLE`.

## Provisioning the role

Creating, altering or dropping a role needs `CREATEROLE` or superuser, and production's application role has neither: it is
a `LOGIN` role that reaches the database owner's objects through membership in the owner role, and superuser is never
inherited through membership. The first version of migration 095 ran `CREATE ROLE` itself, and the 2026.11.0 deploy
crash-looped on it with `permission denied to create role` (bead `phaze-euo4a`). So:

1. **The deployment creates the role, as a role that may create roles (the cluster superuser), before phaze starts.** The
   statements, idempotent and safe to run on every deploy, are in
   [`deploy/postgres-init/10-analytics-ro.sql`](../deploy/postgres-init/10-analytics-ro.sql):

   ```sql
   DO $$
   BEGIN
       IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
           CREATE ROLE analytics_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
       END IF;
       -- ... and ALTER ROLE analytics_ro SET default_transaction_read_only = on, unless it is already set
   END
   $$;
   ```

2. **Then it sets the password**, from its secret store (the value below is a placeholder):

   ```sql
   ALTER ROLE analytics_ro PASSWORD '<password-from-secret-store>';
   ```

3. **Only then does phaze start on the new release.** Its startup runs `alembic upgrade head`, and migration 095 grants to
   the role.

**Ordering is a hard requirement.** If the role does not exist, migrations 095 and 096 **fail**, before any DDL, with:

```
role "analytics_ro" does not exist, and phaze migrations do not create roles. Provision it before upgrading, ...
```

The upgrade's transaction rolls back, so the database stays at its previous revision. Provision the role and restart phaze
to retry. A missing role fails the upgrade on purpose. Skipping the grants instead would leave views that the role, once
it existed, could never read, because nothing re-runs a migration.

Where it runs:

- **Production.** The deployment's provisioning, before the phaze containers start.
- **This repository's compose stack.** `docker-compose.yml` mounts `deploy/postgres-init/` at `/docker-entrypoint-initdb.d`,
  so a **fresh** data volume gets the role at first start. An **existing** volume does not (initdb scripts run once). Run
  the file by hand, for example
  `docker compose exec -T postgres psql -U phaze -d phaze -v ON_ERROR_STOP=1 < deploy/postgres-init/10-analytics-ro.sql`.
- **Tests and benchmarks.** The test harness runs the same file as its superuser (`tests/analytics_role.py`), and
  `just seed-benchmark-db` runs it before migrating. `tests/integration/test_migrations/test_restricted_app_role.py`
  migrates as a role shaped like production's application role, and `tests/shared/test_migrations_issue_no_role_ddl.py`
  fails the build on role DDL in any migration.

The role is cluster-wide while the views are per-database. **`downgrade` never drops the role.** It drops the views (their
grants go with them) and revokes the schema grant. To retire the role, first downgrade or drop every database that still
grants to it, then `DROP ROLE analytics_ro` as a role that may create roles.

## The views

All views are in schema `public`. Identifiers are UUIDs. `file_id` joins every view to `analytics_files.file_id`.
Types are the Postgres types a reader sees; the column order is part of the contract.

### `analytics_files`

One row per file record.

| Column | Type | Meaning |
|---|---|---|
| `file_id` | uuid | File identity (`files.id`) |
| `sha256_hash` | varchar(64) | Content hash |
| `original_path` | text | Path where the file was first seen |
| `original_filename` | text | File name where the file was first seen |
| `current_path` | text | Current path |
| `file_type` | varchar(10) | Extension-derived type, for example `mp3` |
| `file_size` | bigint | Size in bytes |
| `missing_at` | timestamptz | Set when a reconcile found the content nowhere; NULL means "not known to be missing", never "verified present" |
| `created_at` | timestamptz | When the record was created |
| `agent_id` | varchar(64) | The agent that owns the file; joins to `analytics_agent_path_prefixes.agent_id` (added last, migration `096`) |

### `analytics_file_analysis`

The per-file analysis summary. At most one row per file; files never analysed have no row.

| Column | Type | Meaning |
|---|---|---|
| `file_id` | uuid | File identity |
| `bpm` | double precision | File-level tempo |
| `musical_key` | varchar(10) | File-level key |
| `mood` | varchar(50) | File-level mood label |
| `dominant_style` | varchar(50) | Most common coarse-window style label |
| `style` | jsonb | Ranked genre scores, a list of `{name, score}` |
| `fine_windows_analyzed` | integer | Fine windows successfully analysed |
| `fine_windows_total` | integer | Fine windows the file naturally has |
| `coarse_windows_analyzed` | integer | Coarse windows successfully analysed |
| `coarse_windows_total` | integer | Coarse windows the file naturally has |
| `analysis_completed_at` | timestamptz | NULL while analysis is partial or in flight |
| `failed_at` | timestamptz | Set when the analyze stage failed; never set together with `analysis_completed_at` |

A row with `analysis_completed_at IS NULL` is partial: do not treat its aggregates as final.

### `analytics_analysis_windows`

The per-window time series. Many rows per file.

| Column | Type | Meaning |
|---|---|---|
| `window_id` | uuid | Window identity |
| `file_id` | uuid | File identity |
| `tier` | text | `fine` or `coarse` |
| `window_index` | integer | Position within the tier |
| `start_sec` | double precision | Window start, seconds from the start of the file |
| `end_sec` | double precision | Window end, seconds |
| `bpm` | double precision | Fine tier only |
| `musical_key` | varchar(10) | Fine tier only |
| `mood` | varchar(50) | Coarse tier only |
| `style` | varchar(50) | Coarse tier only |
| `danceability` | double precision | Coarse tier only |
| `energy` | double precision | Coarse tier only, in [0, 1] |
| `mood_scores` | jsonb | Coarse tier only; flat `{mood: score}` in a fixed order |

### `analytics_tracklists`

Canonical tracklists only: rows inherited by duplicate propagation (`propagated_from_set_key IS NOT NULL`) are excluded.

| Column | Type | Meaning |
|---|---|---|
| `tracklist_id` | uuid | Tracklist identity |
| `file_id` | uuid | The file the tracklist is linked to; NULL when unlinked |
| `source` | varchar(30) | Where the tracklist came from; new rows are `manual` |
| `status` | varchar(20) | Review status |
| `artist` | text | Set artist |
| `event` | text | Event name |
| `date` | date | Event date |
| `match_confidence` | integer | Link confidence, when auto-linked |
| `auto_linked` | boolean | Whether the file link was made automatically |
| `created_at` | timestamptz | When the row was created |

### `analytics_tracklist_tracks`

The tracks of each canonical tracklist's **latest version** (highest `version_number`). Older versions are not exposed.

| Column | Type | Meaning |
|---|---|---|
| `tracklist_id` | uuid | Tracklist identity |
| `version_id` | uuid | The version the track belongs to |
| `version_number` | integer | That version's number |
| `position` | integer | Track position within the version |
| `artist` | text | Track artist |
| `title` | text | Track title |
| `label` | text | Record label |
| `timestamp` | varchar(20) | Raw timestamp text as stored; not necessarily an offset into the file |
| `timestamp_evidence` | jsonb | Structured evidence for the timestamp, when one was recorded |
| `is_mashup` | boolean | Mashup flag |
| `remix_info` | text | Remix credit |
| `confidence` | double precision | Match confidence |

### `analytics_cue_track_offsets`

The **intrinsic** per-track timestamps of each media's selected CUE source observation: one row per track of the CUE FILE
the operator mapped to that media, exactly as the CUE file states them. Their origin is the CUE file, not the recording,
and they are **not** filtered by availability: a row stays visible after the recording or the CUE changed on disk. Use them
to see what the CUE says; use [`analytics_cue_recording_offsets`](#analytics_cue_recording_offsets) for offsets you may rely on
as positions in the recording. Candidates that are pending or rejected, other observations, and tracks of other FILE entries
are not exposed. Selection is the operator's decision, so a media with no selected CUE has no rows.

| Column | Type | Meaning |
|---|---|---|
| `file_id` | uuid | The media the selection is for |
| `observation_id` | uuid | The selected observation (`provider_source_observations.id`) |
| `selected_at` | timestamptz | When the selection was made |
| `cue_file_ordinal` | integer | The CUE `FILE` entry (1-based) mapped to this media |
| `position` | integer | Track position within the CUE |
| `artist` | text | Track performer |
| `title` | text | Track title |
| `timestamp_original` | text | The `INDEX 01` time as written in the CUE, `MM:SS:FF` |
| `timestamp_kind` | text | `offset`, `clock` or `unknown` |
| `timestamp_precision` | text | `cue_frame_75`, `second`, `minute` or `unknown` |
| `offset_usability` | text | `qualified`, `approximate` or `unusable` |
| `offset_numerator` | bigint | Offset into the file, seconds, as an exact fraction: numerator |
| `offset_denominator` | bigint | Offset denominator; never zero |
| `offset_seconds` | numeric | `offset_numerator / offset_denominator`; NULL when the track has no offset |

`offset_usability` is the intrinsic judgement made when the CUE was parsed; it says nothing about whether the CUE still
describes the recording on disk. For offsets bound to the recording, read `analytics_cue_recording_offsets`.

### `analytics_cue_recording_offsets`

The **target-bound qualified** offsets: the tracks of the mapped CUE FILE whose offset is intrinsically `qualified`, re-bound
to the recording, and only while the operator's selection is **current**. It is computed in SQL from
`provider_recording_selections.target_mapping` and mirrors the application's read-time projection
(`local_source_import._project_tracks` fed by `get_selected_recording_source`), so it returns the same offsets the CUE writer
(`cue_generator`, through `cue_review.cue_tracks_from_selected`) uses. `origin` is `recording:<file_id>`, the recording, not the
CUE file.

A row appears only while **all** of these hold, each one the SQL form of a check the application makes:

- the selection exists and its mapping names a CUE FILE (`cue_file_ordinal`);
- the media is not missing, is a media type, and its `sha256_hash` equals the `media_sha256` the operator reviewed;
- the source observation has status `found`, `incomplete` or `ambiguous`;
- for a `companion` source: the CUE file is present, not marked ambiguous, still linked to this media, its `sha256_hash`
  matches the captured inventory revision and the observation's full revision, and equals the `source_sha256` the operator
  reviewed; for a `remote` source: no later stored read of the same object reported it absent; for a source with no
  channel: the observation status alone;
- the CUE file belongs to the same agent as the media.

Reject, replacement and any sha drift on either file therefore remove the rows without any job running.

**What SQL does not express, stated plainly:** a selection whose source channel is `embedded` (a CUE read from the recording's
own tags) is **not exposed here at all**. Its currency check hashes processed tag text in Python
(`embedded_revision`), which SQL cannot reproduce, so the view under-reports rather than risk reporting an offset the
application would refuse. Read those through the application. A change to the file *content* that keeps the same recorded
`sha256_hash` is outside what any database view can see.

| Column | Type | Meaning |
|---|---|---|
| `file_id` | uuid | The recording the selection is for |
| `observation_id` | uuid | The selected observation |
| `selected_at` | timestamptz | When the selection was made |
| `cue_file_ordinal` | integer | The CUE `FILE` entry (1-based) mapped to this recording |
| `position` | integer | Track position within the CUE |
| `artist` | text | Track performer |
| `title` | text | Track title |
| `timestamp_original` | text | The `INDEX 01` time as written in the CUE, `MM:SS:FF` |
| `timestamp_precision` | text | `cue_frame_75` or `second` for a qualified offset |
| `origin` | text | `recording:<file_id>` |
| `offset_numerator` | bigint | Offset into the recording, seconds, as an exact fraction: numerator |
| `offset_denominator` | bigint | Offset denominator; never zero |
| `offset_seconds` | numeric | `offset_numerator / offset_denominator` |

### `analytics_agent_path_prefixes`

How to turn a stored path into a path under your own mount of the archive. phaze stores **container** paths: a file's
`current_path` starts with one of its owning agent's prefixes (for example `<archive-mount-in-container>`). One row per
agent and prefix. Credential material is never projected. Agents that own no archive paths (compute agents) have no rows.

To open a stored path: join `analytics_files.agent_id` to this view's `agent_id`, keep rows with `active`, take the prefix
that `current_path` starts with (the longest one if several do), and replace it with the mount point where you mounted the
read-only archive view (see [Read-only access to archive audio](operations/readonly-archive-access.md)). A path that starts
with none of its agent's prefixes is not resolvable: skip it rather than guess.

| Column | Type | Meaning |
|---|---|---|
| `agent_id` | varchar(64) | Agent identity |
| `name` | varchar(128) | Agent display name |
| `kind` | varchar(16) | `fileserver` or `compute` |
| `active` | boolean | `false` once the agent's credential was revoked; do not resolve against inactive agents |
| `prefix_ordinal` | integer | 1-based position of the prefix in the agent's scan roots |
| `path_prefix` | text | A container path prefix the agent's stored file paths start with |

## Stability rule

The views are an interface with readers outside phaze, and that makes their **names, column names, column types and
column order a contract**:

1. A migration that adds, drops, renames or retypes a column any `analytics_*` view projects **must update the view in the
   same change**. Dropping or retyping a column under a view makes Postgres refuse the migration, which is the guard working.
2. A column removal or rename is a breaking change: ship a new view name (for example `analytics_files_v2`) beside the old
   one and remove the old one only after readers have moved. Adding a column at the **end** of a view is not breaking.
3. Changing what a view *selects* (for example which tracklist version it exposes) is a contract change too: update this
   page in the same change.
4. `EXPECTED_COLUMNS` in `test_095_analytics_ro_role.py` pins the column contract and the test
   `test_the_docs_describe_every_view_and_column` requires every column to appear in this page, so a change that touches one
   without the other fails.
5. A new view needs its own `SELECT` grant to `analytics_ro` in its migration; the role never gets a blanket grant, and the
   test `test_the_role_holds_select_on_the_views_and_nothing_else` fails on any extra one.

## Network exposure

The role is only as private as the path to it. Postgres here listens on `listen_addresses = '*'`, which exposes every role,
including the application role, to every interface. This page makes recommendations only; nothing about a
deployed host is changed by migration `095`.

1. **Bind address.** Set `listen_addresses` to the loopback address plus the one private interface the consumer reaches, for
   example `listen_addresses = 'localhost, <private-interface-address>'`, instead of `'*'`. Under Docker Compose, publish the
   port on that address only (`<private-interface-address>:5432:5432`), never on `0.0.0.0`.
2. **`pg_hba.conf`.** Give `analytics_ro` its own rule, limited to the one database and the consumer's address, requiring TLS and
   a SCRAM password, and keep it above any broader rule (first match wins):

   ```
   # TYPE    DATABASE   USER          ADDRESS                 METHOD
   hostssl   phaze      analytics_ro  <consumer-address>/32   scram-sha-256
   hostnossl all        analytics_ro  all                     reject
   ```

   The application role keeps its existing rules. Do not add `analytics_ro` to a `host all all` rule.
3. **TLS.** Enable `ssl = on` with a server certificate the consumer trusts (this deployment already distributes an internal
   CA to its agents) and have the consumer connect with `sslmode=verify-full`. `hostssl` in the rule above refuses plaintext
   connections outright.
4. **Limits.** Optionally cap the role with `ALTER ROLE analytics_ro CONNECTION LIMIT <n>` and
   `ALTER ROLE analytics_ro SET statement_timeout = '<duration>'` so a runaway read cannot starve the application.
5. **Verify.** From the consumer, confirm `SELECT current_user` returns `analytics_ro`, that `SELECT * FROM files` is refused
   with `permission denied`, and that a plaintext connection is rejected.
