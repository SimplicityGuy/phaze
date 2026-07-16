<!-- generated-by: gsd-doc-writer -->
# Database

phaze persists all state in PostgreSQL (18+) accessed asynchronously via SQLAlchemy 2.0
(`postgresql+asyncpg://`). Models live in `src/phaze/models/`; schema changes are managed
by Alembic using the async template (`alembic/`). All models inherit a `created_at` /
`updated_at` `TimestampMixin` and share a constraint naming convention defined in
`src/phaze/models/base.py`.

## Schema

| Table                 | Description                                                            |
|-----------------------|-----------------------------------------------------------------------|
| `agents`              | Distributed worker (file-server) identities that own files and scans  |
| `files`               | Central file records; per-stage status is derived on read (no `state` column) |
| `scan_batches`        | Scan operation progress and status (`ScanStatus`)                     |
| `metadata`            | Audio tag metadata (1:1 with `files`)                                  |
| `analysis`            | BPM, key, mood, style results (1:1 with `files`)                       |
| `analysis_window`     | Per-window time-series analysis rows (1:many with `files`, `ON DELETE CASCADE`) |
| `fingerprint_results` | Per-engine fingerprint results (one row per `file_id` + `engine`)     |
| `proposals`           | AI-generated rename/move proposals (`ProposalStatus`)                  |
| `execution_log`       | Append-only audit trail for file rename/move operations               |
| `tag_write_log`       | Append-only audit trail for tag write operations (before/after tags)  |
| `file_companions`     | Many-to-many: companion files to media files                          |
| `tracklists`          | Tracklist metadata (`1001tracklists` or `fingerprint` source)         |
| `tracklist_versions`  | Versioned tracklist snapshots                                         |
| `tracklist_tracks`    | Individual tracks within a version                                    |
| `discogs_links`       | Candidate/accepted Discogs release matches per tracklist track        |
| `cloud_job`           | Per-`file_id` sidecar for the S3 object-staging / cloud-burst leg (1:1 with `files`) |
| `pipeline_stage_control` | Durable per-stage pause/priority operator intent (one row per agent pipeline stage) |
| `scheduling_ledger`   | Durable "this stage was scheduled for this item" record (recovery source of truth)  |
| `route_control`       | Single-row (`id = 'global'`) force-local routing override switch       |
| `dedup_resolution`    | Per-file 1:1 sidecar marking a duplicate resolved to a canonical file (marker-row existence = resolved) |
| `stage_skip`          | Per-`(file_id, stage)` sidecar marking an operator force-skip of an enrich stage      |

### Entity relationships

Foreign keys to `agents` are `ON DELETE RESTRICT` (an agent that owns files/scans cannot be
deleted); `analysis_window` and `file_companions` cascade with their `files` row
(`ON DELETE CASCADE`); the remaining per-file sidecars (`metadata`, `analysis`,
`fingerprint_results`, `proposals`, `cloud_job`, `dedup_resolution`, `stage_skip`) and the
tracklist chain use the default restricting FK (no cascade).

```mermaid
erDiagram
    agents ||--o{ files : "owns (RESTRICT)"
    agents ||--o{ scan_batches : "owns (RESTRICT)"
    files ||--|| metadata : "1:1"
    files ||--|| analysis : "1:1"
    files ||--o{ analysis_window : "CASCADE"
    files ||--o{ fingerprint_results : "per engine"
    files ||--o{ proposals : "rename/move"
    files ||--o| cloud_job : "0..1 sidecar"
    files ||--o{ file_companions : "CASCADE"
    tracklists ||--o{ tracklist_versions : "versions"
    tracklist_versions ||--o{ tracklist_tracks : "tracks"
    tracklist_tracks ||--o{ discogs_links : "match candidates"
    tracklists }o--o| files : "optional link"
```

### Agent attribution

`files` and `scan_batches` each carry a non-null `agent_id` (`String(64)`) that foreign-keys
to `agents.id` with `ON DELETE RESTRICT`. New rows default to the seeded
`legacy-application-server` agent. Uniqueness on `files` is the composite
`(agent_id, original_path)` — the same path may exist under different agents. `scan_batches`
enforces a partial unique index allowing at most one `status = 'live'` watcher batch per agent.

### Proposal idempotency

`proposals` carries a partial UNIQUE index `uq_proposals_file_id_pending` on `file_id`
`WHERE status = 'pending'` (model `src/phaze/models/proposal.py`, migration `019`). It
structurally guarantees at most one PENDING proposal per file (D-04). This index is the
`ON CONFLICT` target for `services.proposal.store_proposals`' upsert
(`on_conflict_do_update` with `index_elements=["file_id"]` and
`index_where=status == 'pending'`): re-running proposal generation overwrites the single
pending row in place rather than accumulating duplicates. Because the index predicate is
scoped to `status = 'pending'`, rows in any other state (`approved`, `executed`, `rejected`,
`failed`) fall outside the index and are never a conflict target — human approvals are
structurally protected from being overwritten by a re-run.

### Derived per-stage status

There is **no `files.state` column and no file-level state enum** — Phase 90 dropped the
`state` column, the file-level state `StrEnum`, and the `ix_files_state` index (in the
pre-flatten chain's migration `039_drop_files_state_column`, now folded into the `039`
baseline schema). A file's status is instead **derived on read**, per stage,
from its output tables (`metadata`, `fingerprint_results`, `analysis`, `proposals`,
`execution_log`), the `cloud_job` sidecar, and the `dedup_resolution` marker.

- `Stage` (`src/phaze/enums/stage.py`, 7 stages): `metadata`, `fingerprint`, `analyze`,
  `tracklist`, `propose`, `review`, `apply`.
- `Status` (`src/phaze/enums/stage.py`, 5 states): `not_started`, `in_flight`, `done`,
  `skipped`, `failed`, resolved under the precedence ladder
  `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started`. The durable `scheduling_ledger` is the
  authoritative `in_flight` source. The DB-free resolver `resolve_status` and its SQL twin
  `services/stage_status.py` (`stage_status_case`) are locked 1:1 by an equivalence test.
- `CloudJobStatus` (`cloud_job.py`): `awaiting`, `uploading`, `uploaded`, `submitted`,
  `running`, `succeeded`, `failed` — tracks the long-file cloud-burst / tiered-drain detour
  off `analyze` on the standalone `cloud_job` sidecar row (not a file state).
- `ScanStatus` (`scan_batch.py`): `running`, `completed`, `failed`, `live`.
- `ProposalStatus` (`proposal.py`): `pending`, `approved`, `rejected`, `executed`, `failed`.
- `TagWriteStatus` (`tag_write_log.py`): `completed`, `failed`, `discrepancy`.
- `ExecutionStatus` is defined in `src/phaze/enums/execution.py` and re-exported from
  `models/execution.py`.

The conceptual per-file stage progression (each node's status is derived, never stored):

```mermaid
flowchart TD
    discovered --> metadata
    metadata --> fingerprint
    fingerprint --> analyze
    fingerprint -.long file, cloud/compute routed.-> cloud_job[(cloud_job sidecar)]
    cloud_job --> analyze
    analyze --> propose
    propose --> review
    propose -.duplicate.-> dedup_resolution[(dedup_resolution)]
    review --> apply
```

### Full-text search

PostgreSQL `GENERATED ALWAYS ... STORED` `tsvector` columns (`search_vector`) exist on
`files`, `metadata`, and `tracklists`, each backed by a GIN index. The schema also enables the
`pg_trgm` extension and has trigram GIN indexes for `ILIKE` partial matching. `discogs_links`
carries its own GIN FTS index on denormalized artist/title. (Originated in the pre-flatten
chain's migration 009; now part of the `039` baseline schema.)

## Migrations

Schema is managed by Alembic with the async template (`alembic/env.py` overrides
`sqlalchemy.url` from application settings, so no URL is hard-coded in `alembic.ini`).

**As of Phase 102 (phaze-8hfu), the entire linear chain was flattened into a single baseline
file: `alembic/versions/039_baseline_schema.py`.** It reuses revision id `039` with
`down_revision = None` — so a production database already stamped `039` by the pre-flatten
chain treats the next `upgrade head` as a no-op, while a fresh (CI / test / new) database
builds the entire current schema, plus required seed rows (`pipeline_stage_control` per-stage
rows, the `route_control` `'global'` row), from this one file. The embedded DDL is a
normalized `pg_dump --schema-only` of the real pre-flatten chain's output
(`scripts/normalize_schema_dump.py`), so every non-metadata artifact the chain accreted
(partial indexes, CHECK constraints, generated `tsvector` columns + GIN/trigram indexes, the
`pg_trgm` extension) is preserved byte-faithfully. Fidelity is guarded going forward by
`tests/integration/test_migrations/test_baseline_schema.py`.

```bash
just db-upgrade              # Apply all pending migrations (alembic upgrade head)
just db-revision "message"   # Create new migration (alembic revision --autogenerate)
just db-current              # Show current migration (alembic current)
just db-downgrade            # Roll back one migration (alembic downgrade -1)
just db-history              # Show migration history (alembic history)
```

`db-revision` autogenerates from model changes — all models are imported in
`src/phaze/models/__init__.py` so Alembic can discover them. New migrations now build on top
of the `039` baseline rather than the retired `001`-`039` chain.
