# Phase 24: Schema Foundation & Agent Registry - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning

<domain>
## Phase Boundary

The database can model who owns each file and which agent originated each scan. A new `agents` table records each file server's identity (`id`, `name`, `token_hash`, `scan_roots`, `created_at`, `last_seen_at`, `revoked_at`). `FileRecord.agent_id` and `ScanBatch.agent_id` are non-null string columns referencing agents. The file uniqueness invariant moves from `(original_path)` to `(agent_id, original_path)`. A two-step Alembic migration adds the new columns and table, seeds a `legacy-application-server` agent reflecting the current `SCAN_PATH`, backfills every existing FileRecord and ScanBatch to it, creates one sentinel `LIVE` ScanBatch per agent, and only then enforces NOT NULL and swaps the unique constraint. Existing v3.0 data survives end-to-end and the schema can be downgraded cleanly to the v3.0 schema on an unmigrated test DB.

</domain>

<decisions>
## Implementation Decisions

### Agent ID Format & FK Semantics
- **D-01:** `agents.id` is a kebab-case slug string (e.g. `legacy-application-server`, `fileserver-01`). Operator picks the id at registration. The slug flows through as the SAQ queue name `phaze-agent-<id>` and into log lines, so it must stay human-readable.
- **D-02:** Column type for `agents.id`, `files.agent_id`, and `scan_batches.agent_id` is `VARCHAR(64)`. A CHECK constraint enforces `[a-z0-9-]+` only (lowercase letters, digits, hyphens). No leading/trailing hyphens or double hyphens — keep the validation regex simple but safe for Redis keys, URL paths, and shell-quoted commands.
- **D-03:** `files.agent_id` and `scan_batches.agent_id` are **real FOREIGN KEYs** to `agents.id`. Postgres enforces referential integrity; orphan rows are impossible. `ON DELETE` policy: `RESTRICT` (deleting an agent that still owns files is a hard error — operator must revoke + reassign or hard-delete files first). This matches the existing `fk_files_batch_id_scan_batches` pattern.
- **D-04:** The `legacy-application-server` row is inserted **inside** the upgrade migration (revision 012), before any backfill UPDATE runs. The migration is self-contained and re-runnable — no separate `just seed-legacy-agent` step is required for the operator.

### Legacy Agent Backfill
- **D-05:** `scan_roots` for the legacy agent is populated by **reading `SCAN_PATH` from the environment at migration time** (verified against `src/phaze/config.py:24` and `docker-compose.yml:12` — see Errata below). Stored as a JSONB array: `["<value of SCAN_PATH>"]`. Falls back to `["/data/music"]` if the env var is unset. The migration logs which value it used so the operator has an audit trail.
- **D-06:** The legacy agent is **born revoked**: `token_hash = NULL` and `revoked_at = NOW()` at insert time. It exists purely for FK integrity over pre-existing data; no HTTP traffic should ever authenticate as it. Phase 25's auth middleware will reject any request whose resolved agent has `revoked_at IS NOT NULL`, so the legacy row is unreachable by design.
- **D-07:** `token_hash` is **nullable** on the `agents` table. Combined with the auth check, NULL means "no usable credential." This lets the legacy agent exist credential-less and leaves room for future use cases (e.g., temporarily disabled agents).
- **D-08:** Every pre-existing `FileRecord` and every pre-existing `ScanBatch` is attributed to the legacy agent during backfill. Both new columns are NOT NULL once enforced, so something must be set for every row; the legacy agent covers all of them.

### Sentinel LIVE ScanBatch
- **D-09:** The sentinel is distinguished by a **new `ScanStatus.LIVE` enum value** added alongside `RUNNING`, `COMPLETED`, `FAILED`. Querying for an agent's sentinel is `WHERE agent_id = ? AND status = 'live'` (stored value is lowercase `'live'` — see Errata below). This keeps the marker in a dedicated, indexable column and matches the existing `ScanStatus` enum pattern in `src/phaze/models/scan_batch.py`.
- **D-10:** The sentinel's `scan_path` value is the literal string `"<watcher>"`. Human-readable in the admin UI ("this batch holds watcher-originated files"), doesn't lie about being a real filesystem path, and avoids forcing `scan_path` to become nullable.
- **D-11:** Each agent's LIVE sentinel is created at **agent-registration time**:
  - For the **legacy agent**, the upgrade migration inserts both the agents row and its LIVE sentinel ScanBatch in revision 012.
  - For **new agents going forward** (Phase 25+), the agent-registration code path inserts the LIVE sentinel as part of the same transaction that inserts the agent. Phase 24 does not need to implement the future code path, but the schema must support it.
- **D-12:** Idempotency on the sentinel is enforced by a **partial unique index**:
  ```sql
  CREATE UNIQUE INDEX uq_scan_batches_agent_id_live
    ON scan_batches (agent_id) WHERE status = 'live';
  ```
  Postgres guarantees at most one LIVE batch per agent at the DB level. Re-applying the migration or re-registering an agent cannot duplicate the sentinel. This directly satisfies success criterion #4 ("one sentinel `LIVE` ScanBatch exists per registered agent and is reused").

### Migration Shape & Rollback
- **D-13:** "Two-step" means **two separate Alembic revisions**:
  - **012 — additive + backfill:** create `agents` table, insert legacy agent + its LIVE sentinel, add nullable `files.agent_id` and `scan_batches.agent_id`, add FKs, add new `ScanStatus.LIVE` enum value, add `uq_scan_batches_agent_id_live` partial index, run backfill UPDATEs.
  - **013 — tighten constraints:** enforce NOT NULL on both `agent_id` columns, drop `uq_files_original_path`, create `uq_files_agent_id_original_path` composite unique constraint.
  Each step is independently revertable. The operator can pause between them if backfill on a real 200K-file DB takes longer than expected.
- **D-14:** Backfill is written as **pure SQL via `op.execute(...)`** — single `UPDATE files SET agent_id = 'legacy-application-server' WHERE agent_id IS NULL`, same for `scan_batches`. Atomic, no SQLAlchemy model imports needed, no model-version coupling between the migration file and current model code. Schema-only INSERTs for the legacy agent + sentinel also use raw SQL to avoid pulling models into the migration.
- **D-15:** **Index strategy is minimal**:
  - Drop `uq_files_original_path` (single-column unique index from migration 002).
  - Create `uq_files_agent_id_original_path` composite unique index — Postgres can use the leading column for `agent_id` filters, so no separate `ix_files_agent_id` is needed.
  - Add `ix_scan_batches_agent_id` (no composite to lean on there).
  - No other new indexes in Phase 24. Phase 28 dispatch query patterns can add what they need later.
- **D-16:** **Downgrade fails loudly if duplicates exist.** If `(original_path)` no longer uniquely identifies a file because the same path lives under multiple agents, the downgrade raises an error and tells the operator to resolve manually. Silent dedup is forbidden — this is an irreplaceable personal collection. Document the rollback procedure in the migration docstring. The full downgrade path (013 → 012 → pre-v4.0 schema) is implemented and tested against an unmigrated DB.

### Claude's Discretion
- Exact regex for the CHECK constraint on `agent_id` columns (e.g. `^[a-z0-9]+(-[a-z0-9]+)*$` vs simpler `^[a-z0-9-]+$`) — pick whichever Postgres-renderable form is cleanest.
- Whether to express the CHECK constraint in SQLAlchemy model `__table_args__`, in the Alembic migration, or both — keep them consistent.
- New `Agent` SQLAlchemy model file layout (likely `src/phaze/models/agent.py`) and the `relationship()` declarations for back-references to FileRecord / ScanBatch.
- Pydantic schemas for any agent-related types added in Phase 24 (likely none — schemas are a Phase 25 concern when the HTTP surface lands).
- Test fixtures for "DB with legacy agent + sentinel pre-seeded" — conftest helper shape, naming, and scope.
- Whether to also add a `ScanStatus.LIVE` mapping in `src/phaze/models/scan_batch.py` immediately or wait for Phase 27 to wire it into the watcher — recommend doing it in Phase 24 so the enum stays in sync with the DB constraint.
- Logging format and verbosity of the backfill step (how many rows touched, env var resolution, etc.) — operator-visible but not a contract.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project & Milestone Context
- `.planning/PROJECT.md` — v4.0 milestone scope, key decisions table, constraints
- `.planning/REQUIREMENTS.md` §"Data Model & Migration" — DATA-01, DATA-02, DATA-03, DATA-04 (the four requirements this phase satisfies)
- `.planning/ROADMAP.md` §"Phase 24: Schema Foundation & Agent Registry" — goal and success criteria
- `.planning/STATE.md` §"Accumulated Context → Decisions → v4.0" — locked pre-roadmap decisions (HTTP-only boundary, per-agent queue naming, etc.)

### Existing Models to Modify
- `src/phaze/models/base.py` — `Base` DeclarativeBase, `TimestampMixin`, naming convention dict (`ix_`, `uq_`, `fk_`, `ck_`, `pk_`); every new model and constraint must follow these prefixes
- `src/phaze/models/file.py` — `FileRecord` model + `FileState` enum; the `__table_args__` block holding `uq_files_original_path` is the target of the constraint swap
- `src/phaze/models/scan_batch.py` — `ScanBatch` model + `ScanStatus` enum; `ScanStatus.LIVE` gets added here

### Existing Migrations as Pattern Reference
- `alembic/versions/002_add_scan_batches_and_unique_path.py` — pattern for creating the `uq_files_original_path` index that Phase 24 drops; same migration also shows the FK shape that `fk_files_agent_id_agents` should mirror
- `alembic/versions/011_add_tag_write_log.py` — most recent migration; reference for current `op.create_table` style, FK declaration, server defaults, index creation, and downgrade structure

### Code That Reads/Writes the Affected Schema (must remain working after migration)
- `src/phaze/models/__init__.py` — central model exports; add `Agent` here
- `src/phaze/database.py` — async engine pool; no changes expected in Phase 24 but verify nothing breaks
- `src/phaze/services/ingestion.py` — currently inserts FileRecord rows; will need to set `agent_id` once Phase 25+ wires it up (Phase 24 does not change call sites, only the column definition + backfill)
- `src/phaze/services/dedup.py` — relies on `(original_path)` uniqueness; verify that switching to `(agent_id, original_path)` doesn't break dedup queries (they run per-agent or globally?)
- `src/phaze/services/hashing.py`, `src/phaze/tasks/scan.py` — same caveat: any query joining on `original_path` may need to also filter on `agent_id`

### Constants / Configuration
- `src/phaze/config.py` — `Settings` class; `PHAZE_SCAN_PATH` (or equivalent) is what the migration reads to populate `legacy-application-server.scan_roots`
- `CLAUDE.md` — Python 3.13, uv, mypy strict, ruff config, 150-char lines, pre-commit hook expectations

### Prior Phase Context (Patterns to Follow)
- `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-CONTEXT.md` — original FileRecord shape and dedup model
- `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` — SAQ queue / worker patterns (queue naming convention will matter for Phase 26)
- `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-CONTEXT.md` — most recent pattern for "add columns + backfill" migration shape
- `.planning/milestones/v3.0-phases/20-tag-writing/20-CONTEXT.md` — TagWriteLog audit-trail model; reference for new-model CONTEXT structure

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`Base` + `TimestampMixin`** in `src/phaze/models/base.py`: every new model (`Agent`) uses these. Naming convention dict gives us free `pk_`, `uq_`, `fk_`, `ck_`, `ix_` prefixes — must not be bypassed.
- **`postgresql.UUID(as_uuid=True)`** pattern: used in `FileRecord.id`, `ScanBatch.id`, etc. `Agent.id` is **NOT** a UUID — it's `VARCHAR(64)` per D-01.
- **JSONB columns**: precedent in `FileMetadata.raw_tags`, `TagWriteLog.before_tags/after_tags`, `DiscogsLink.metadata`. `Agent.scan_roots` follows the same pattern (`sa.dialects.postgresql.JSONB`).
- **`StrEnum` for status fields**: `ScanStatus`, `FileState`, others. `ScanStatus` gets a new `LIVE` value.
- **Alembic revision numbering**: zero-padded three-digit strings (`"011"` → next is `"012"`, then `"013"`).
- **`op.execute(sa.text(...))`** for SQL backfill: precedent in earlier migrations (e.g., 009 search vectors).

### Established Patterns
- All models use `TimestampMixin` (`created_at`, `updated_at`); `Agent` follows this, but adds `last_seen_at` and `revoked_at` as additional nullable timestamp columns.
- Constraint names follow the convention dict in `base.py`. The migration must use names like `fk_files_agent_id_agents`, `uq_files_agent_id_original_path`, `ck_agents_id_charset`, `uq_scan_batches_agent_id_live`.
- Foreign keys declared with `sa.ForeignKey("table.col")` in the column definition AND mirrored in `__table_args__` for the named constraint (see `files.batch_id` in `file.py`).
- Indexes declared in `__table_args__` alongside the column definitions, not in separate `Index(...)` calls.

### Integration Points
- **New model:** `src/phaze/models/agent.py` — `Agent` class with all columns from D-01 through D-08.
- **Modified models:**
  - `src/phaze/models/file.py` — add `agent_id: Mapped[str]` column with FK to `agents.id` and a `relationship("Agent", ...)` if planner wants the reverse nav; update `__table_args__` to swap the unique constraint.
  - `src/phaze/models/scan_batch.py` — add `agent_id: Mapped[str]` column, add `ScanStatus.LIVE`, declare the partial unique index for the sentinel.
- **Two new Alembic migrations:**
  - `alembic/versions/012_add_agents_table_and_backfill.py`
  - `alembic/versions/013_enforce_agent_id_not_null_and_swap_uniqueness.py`
- **Model exports:** `src/phaze/models/__init__.py` — add `Agent` to the exported names so downstream `phaze.models import Agent` works.
- **Tests:** new test module(s) covering: legacy agent row inserted with `scan_roots` from env, partial unique index prevents duplicate LIVE sentinels, unique constraint allows same `original_path` under different `agent_id`, downgrade roundtrip on a fresh DB succeeds, downgrade fails loudly when `(original_path)` dupes exist.

</code_context>

<specifics>
## Specific Ideas

- The legacy agent's name is exactly `legacy-application-server` (matches the slug). The string flows into log lines, the eventual Agents admin page, and operator-facing migration output, so it should read as obviously-not-a-real-agent.
- The sentinel `scan_path` value is the literal string `"<watcher>"` (angle brackets included) so it visually distinguishes from real filesystem paths in the admin UI and grep output.
- `scan_roots` is always a JSONB array of strings, never a single string. Even with one root, `["/music"]`. Future agents may have multiple roots.
- The auth middleware contract (Phase 25's job, but worth noting here): any agent row with `revoked_at IS NOT NULL` is unauthenticated. Phase 24's legacy agent relies on this being true.
- Reading the env var inside the migration is fine even though migrations should ideally be pure — this is a one-time backfill of historical state and the env source-of-truth is what the v3.0 worker has actually been scanning.

</specifics>

<deferred>
## Deferred Ideas

- **Agent self-registration / multi-tenant onboarding** — OPS-06 in Future Requirements. v4.0 today: operator pre-seeds agent rows via a Phase 25/29 admin endpoint.
- **mTLS in addition to bearer tokens** — OPS-05; out of scope for this milestone.
- **Cross-file-server fingerprint matching** — XAGENT-01; v4.0 documents this as a known limitation.
- **Watcher catch-up / delete / move detection** — WATCH-05/06/07; v4.0 watcher only handles `created` events.
- **Agent metrics scraping endpoint** — OPS-07; deferred.
- **Reverse `Agent.files` / `Agent.scan_batches` relationships** — planner can decide whether to add `lazy="noload"` back-references in the Agent model. Not strictly needed for Phase 24, no consumer in this phase.
- **Per-agent `scan_path` validation against `agents.scan_roots`** — Phase 27's job (the scan endpoint should refuse paths outside the agent's roots). Not a Phase 24 schema concern.

</deferred>


<errata>
## Errata

Three minor misprints in the original CONTEXT.md draft were discovered during planner-checker review and corrected in-place above. The originals are preserved here for diff history.

- **D-05 (env var name):** Originally written as "reading `PHAZE_SCAN_PATH` (or equivalent)". The actual ground-truth env var in this codebase is `SCAN_PATH` (verified via `src/phaze/config.py:24` `scan_path: str = "/data/music"` and `docker-compose.yml:12` `${SCAN_PATH:-/data/music}`). All Phase 24 plans correctly use `SCAN_PATH`.

- **D-05 (fallback path):** Originally written as `["/music"]`. The actual default in `src/phaze/config.py:24` and `docker-compose.yml:12` is `/data/music`. All Phase 24 plans correctly use `/data/music`.

- **D-09 / D-12 (SQL string literal casing):** Originally written as `'LIVE'` (uppercase) in the WHERE-clause SQL examples. The actual stored value follows `ScanStatus.LIVE = "live"` (lowercase, matching existing enum-value casing `'running'`, `'completed'`, `'failed'`). All Phase 24 plans correctly use `'live'` lowercase in SQL predicates. The `ScanStatus.LIVE` Python name remains uppercase per StrEnum convention; only the stored/queried value is lowercase.
</errata>

---

*Phase: 24-schema-foundation-agent-registry*
*Context gathered: 2026-05-11*
