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
| `files`               | Central file records with a `FileState` state machine                 |
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

### State enums

- `FileState` (`src/phaze/models/file.py`): `discovered`, `metadata_extracted`,
  `fingerprinted`, `analyzed`, `proposal_generated`, `approved`, `rejected`, `executed`,
  `failed`, `duplicate_resolved`, `moved`, `unchanged`.
- `ScanStatus` (`scan_batch.py`): `running`, `completed`, `failed`, `live`.
- `ProposalStatus` (`proposal.py`): `pending`, `approved`, `rejected`, `executed`, `failed`.
- `TagWriteStatus` (`tag_write_log.py`): `completed`, `failed`, `discrepancy`.
- `ExecutionStatus` is defined in `src/phaze/enums/execution.py` and re-exported from
  `models/execution.py`.

### Full-text search

Migration 009 adds PostgreSQL `GENERATED ALWAYS ... STORED` `tsvector` columns
(`search_vector`) to `files`, `metadata`, and `tracklists`, each backed by a GIN index. It
also enables the `pg_trgm` extension and creates trigram GIN indexes for `ILIKE` partial
matching. `discogs_links` carries its own GIN FTS index on denormalized artist/title.

## Migrations

Schema is managed by Alembic with the async template (`alembic/env.py` overrides
`sqlalchemy.url` from application settings, so no URL is hard-coded in `alembic.ini`).
Migrations run sequentially from `001` through `019` in `alembic/versions/`; `019` is the
current head.

```bash
just db-upgrade              # Apply all pending migrations (alembic upgrade head)
just db-revision "message"   # Create new migration (alembic revision --autogenerate)
just db-current              # Show current migration (alembic current)
just db-downgrade            # Roll back one migration (alembic downgrade -1)
just db-history              # Show migration history (alembic history)
```

`db-revision` autogenerates from model changes — all models are imported in
`src/phaze/models/__init__.py` so Alembic can discover them.

### Recent migrations

| Rev | Summary                                                                                  |
|-----|------------------------------------------------------------------------------------------|
| 009 | Add `search_vector` GENERATED tsvector columns + GIN/trigram indexes; enable `pg_trgm`    |
| 010 | Create `discogs_links` table with status/track indexes and a GIN FTS index               |
| 011 | Create `tag_write_log` table (before/after JSONB tags) with file/status indexes          |
| 012 | Create `agents` table, seed legacy agent + LIVE sentinel batch, add nullable `agent_id` + FKs, backfill |
| 013 | Set `agent_id` NOT NULL; swap `files` uniqueness from `original_path` to `(agent_id, original_path)` |
| 014 | Add `agents.last_status` JSONB column + partial token-hash index (`WHERE revoked_at IS NULL`) |
| 015 | Add nullable tz-aware `scan_batches.completed_at` terminal-timestamp column               |
| 016 | Backfill `scan_batches.completed_at = updated_at` for terminal rows with a NULL value     |
| 017 | Add nullable `scan_batches.last_progress_at` heartbeat column + backfill from `updated_at` |
| 018 | Create `analysis_window` table (per-window time-series rows) with composite/partial/label indexes |
| 019 | Dedupe existing pending proposals, then add partial unique index `uq_proposals_file_id_pending` |

Migration 013's downgrade fails loudly if the same `original_path` now exists under multiple
agents — duplicates must be resolved manually before rolling back, since silent dedup is
forbidden for an irreplaceable personal collection.

Migration 019 runs two ordered ops in `upgrade()`: it first collapses pre-existing duplicate
PENDING proposals to one-per-file (keeping the most-recent `created_at`), then creates the
partial unique index — the dedupe MUST run first or `CREATE UNIQUE INDEX` aborts on the live
archive's duplicates. Its `downgrade()` only drops the index; the dedupe DELETE is not
reversible.
