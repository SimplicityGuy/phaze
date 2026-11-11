# Read-only analytics access

A read-only consumer of phaze's data connects as the dedicated Postgres role **`analytics_ro`**, never as the
application role. The role can `SELECT` from six `analytics_*` views and from nothing else: no base table, no write, no DDL.
Migration `095_analytics_ro_role.py` creates the role and the views; `tests/integration/test_migrations/test_095_analytics_ro_role.py`
connects as the role and proves each of those claims.

## What the role is

| Property | Value |
|---|---|
| Name | `analytics_ro` |
| Attributes | `LOGIN`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`, `NOINHERIT` |
| Grants | `USAGE` on schema `public`; `SELECT` on the six views below. Nothing on any base table |
| Role setting | `default_transaction_read_only = on`, a second wall behind the missing write grants |
| Password | **None until the operator sets one.** A migration is the wrong place for a secret |

The views run with their owner's privileges (the Postgres default for a view), which is why the role needs no base-table
privilege: it can see exactly the columns a view projects and no others. Columns that are large or internal (`features`
JSONB on `analysis` and `analysis_window`, `error_message`, source URLs, review and queue state) are deliberately not projected.

The role is cluster-wide while the views are per-database. `upgrade` creates the role only if it does not exist;
`downgrade` drops the views and the grants and drops the role only when no other database still depends on it.

### Setting the password

Run once per environment, as a database superuser, with the secret taken from the operator's secret store (the value
below is a placeholder):

```sql
ALTER ROLE analytics_ro PASSWORD '<password-from-secret-store>';
```

Until then the role cannot authenticate with a password.

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

The per-track timestamps of each media's **selected** CUE source observation: one row per track of the CUE FILE the
operator mapped to that media. Candidates that are pending or rejected, other observations, and tracks of other FILE
entries are not exposed. Selection is the operator's decision, so a media with no selected CUE has no rows.

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

`offset_usability` is the intrinsic judgement made when the CUE was parsed. A consumer that needs only trustworthy
offsets filters on `offset_usability = 'qualified'`; treat `approximate` and `unusable` offsets as unusable for slicing.

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
including the superuser-level application role, to every interface. This page makes recommendations only; nothing about a
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
