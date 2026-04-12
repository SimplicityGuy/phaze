# Database

## Schema

| Table                 | Description                                       |
|-----------------------|---------------------------------------------------|
| `files`               | Central file records with state machine           |
| `scan_batches`        | Scan operation progress tracking                  |
| `metadata`            | Audio tag metadata (1:1 with files)               |
| `analysis`            | BPM, key, mood, style results (1:1 with files)    |
| `proposals`           | AI-generated rename/move proposals                |
| `execution_log`       | Append-only audit trail for file operations       |
| `file_companions`     | Many-to-many: companion files to media files      |
| `fingerprint_results` | Per-engine fingerprint results (audfprint/panako) |
| `tracklists`          | Tracklist metadata from 1001Tracklists            |
| `tracklist_versions`  | Versioned tracklist snapshots                     |
| `tracklist_tracks`    | Individual tracks within a version                |

## Migrations

Managed by Alembic with async template:

```bash
just db-upgrade              # Apply all pending migrations
just db-revision "message"   # Create new migration (autogenerate)
just db-current              # Show current migration
just db-downgrade            # Roll back one migration
just db-history              # Show migration history
```
