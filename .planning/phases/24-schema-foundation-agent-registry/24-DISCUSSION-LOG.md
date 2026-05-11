# Phase 24: Schema Foundation & Agent Registry - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-11
**Phase:** 24-schema-foundation-agent-registry
**Areas discussed:** Agent ID format & FK semantics, Legacy agent backfill details, Sentinel LIVE ScanBatch design, Migration shape / indexes / rollback

---

## Agent ID format & FK semantics

### Q1: What format should `agents.id` take?

| Option | Description | Selected |
|--------|-------------|----------|
| Kebab-case slug | e.g. `legacy-application-server`, `fileserver-01`. Human-readable, queue names like `phaze-agent-fileserver-01` stay readable. | ✓ |
| UUID string | Globally unique, no naming collisions, but queue names become opaque. | |
| Short ID (e.g. `lux`, `nox`) | Compact, terminal-friendly, but tight on collision space and feels arbitrary. | |

**User's choice:** Kebab-case slug (Recommended)
**Notes:** Aligns with the carried-forward decision to prefer role-based names over hostnames in planning artifacts (feedback_generic_server_names.md).

### Q2: Should `files.agent_id` and `scan_batches.agent_id` be real FOREIGN KEY constraints?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, real FKs | Referential integrity enforced by Postgres. Standard pattern matches existing ScanBatch <- FileRecord FK. | ✓ |
| Loose strings, no FK | More flexible (can pre-stamp agent_id before agents row exists) but lets bad data sneak in. | |

**User's choice:** Yes, real FKs (Recommended)
**Notes:** Followed up in CONTEXT.md with `ON DELETE RESTRICT` as the safe default — deleting an agent that still owns files should be a hard error.

### Q3: What naming constraints should `agents.id` enforce?

| Option | Description | Selected |
|--------|-------------|----------|
| VARCHAR(64), `[a-z0-9-]` only, lowercase | Safe for Redis queue names, URL paths, log lines. CHECK constraint or app-level validation. | ✓ |
| VARCHAR(128), no charset constraint | Permissive — lets operator pick anything. Risk: weird chars break Redis keys or shell-quoted commands. | |
| You decide | Pick what fits the existing model conventions. | |

**User's choice:** VARCHAR(64), `[a-z0-9-]` only, lowercase (Recommended)
**Notes:** Exact regex shape left as Claude's discretion (D-01 in CONTEXT.md).

### Q4: When does the `legacy-application-server` agents row get inserted?

| Option | Description | Selected |
|--------|-------------|----------|
| Inside the upgrade migration | Migration creates table, inserts legacy row, then backfills. Self-contained and re-runnable. | ✓ |
| Separate seed step (justfile / CLI) | Migration only creates schema; operator runs a seed command before backfill. More operator steps. | |

**User's choice:** Inside the upgrade migration (Recommended)

---

## Legacy agent backfill details

### Q1: Where does the legacy agent's `scan_roots` value come from at migration time?

| Option | Description | Selected |
|--------|-------------|----------|
| Read SCAN_PATH from env at migration time | Migration reads PHAZE_SCAN_PATH and stores `["$SCAN_PATH"]` in scan_roots jsonb. Falls back to `["/music"]` if unset. | ✓ |
| Hardcode `["/music"]` | Simpler but wrong if the operator has been running with a different SCAN_PATH. | |
| `alembic -x scan_path=...` | Operator passes the path explicitly. Most explicit but adds an operator step. | |
| Empty array `[]` | Don't pretend to know. Legacy agent is FK-only; no live scans will originate from it. | |

**User's choice:** Read SCAN_PATH from env at migration time (Recommended)
**Notes:** Captured in CONTEXT.md D-05; migration logs the resolved value for audit.

### Q2: Should the legacy agent have a usable `token_hash` or be born revoked?

| Option | Description | Selected |
|--------|-------------|----------|
| Born revoked: `token_hash` NULL, `revoked_at = NOW()` | Legacy agent exists purely for FK integrity. No HTTP traffic ever authenticates as it. | ✓ |
| Real generated token, logged to migration output | Migration generates a random token, hashes it, prints once. | |
| Real token from env (`PHAZE_LEGACY_AGENT_TOKEN`) | Operator pre-generates a token. | |

**User's choice:** Born revoked (Recommended)
**Notes:** Phase 25's auth middleware will reject revoked agents — legacy row is unreachable by design.

### Q3: Is `token_hash` nullable in the agents table?

| Option | Description | Selected |
|--------|-------------|----------|
| Nullable | Lets legacy agent live without a token. NULL token_hash plus app-side check that 'revoked or no hash' = unauthenticated. | ✓ |
| NOT NULL with sentinel value for legacy | Stricter schema; legacy gets a never-matching sentinel. Cleaner type but uglier data. | |

**User's choice:** Nullable (Recommended)

### Q4: Which existing rows get attributed to the legacy agent during backfill?

| Option | Description | Selected |
|--------|-------------|----------|
| Every FileRecord + every ScanBatch | All historical rows point at legacy agent. Matches success criterion #3. | ✓ |
| FileRecord only; ScanBatch gets agent_id but no LIVE sentinel for legacy | Existing ScanBatches retain their original scan_path but get the legacy agent_id. | |
| You decide | Pick what matches the FK and not-null requirements. | |

**User's choice:** Every FileRecord + every ScanBatch (Recommended)

---

## Sentinel LIVE ScanBatch design

### Q1: How is the sentinel LIVE ScanBatch distinguished from a normal scan batch?

| Option | Description | Selected |
|--------|-------------|----------|
| New `ScanStatus.LIVE` enum value | Add LIVE alongside RUNNING/COMPLETED/FAILED. Schema-clean, fits existing enum pattern. | ✓ |
| `scan_path` = sentinel string 'LIVE' | No enum change; the sentinel's scan_path holds the magic string. Mixes a marker into a free-form field. | |
| New boolean column `is_sentinel` | Explicit flag; adds a column for a single use case. | |

**User's choice:** New `ScanStatus.LIVE` enum value (Recommended)

### Q2: What `scan_path` value does the sentinel LIVE batch store?

| Option | Description | Selected |
|--------|-------------|----------|
| Literal string `<watcher>` | Human-readable in admin UI. Doesn't lie about being a real path. | ✓ |
| NULL (requires making `scan_path` nullable) | Schema-pure but `scan_path` is currently NOT NULL and code may assume non-null. | |
| Agent's first `scan_root` | Reuses real path data but misleading when an agent has multiple roots. | |

**User's choice:** Literal string `<watcher>` (Recommended)

### Q3: When does each agent's sentinel LIVE ScanBatch get created?

| Option | Description | Selected |
|--------|-------------|----------|
| In the upgrade migration for the legacy agent; on agent registration for new agents | Migration creates legacy agent + its sentinel. Phase 25's agent-registration code creates the sentinel for new agents. | ✓ |
| Lazily on first watcher event | First file upsert from a given agent creates the sentinel if missing. Defers creation to Phase 25/27 code. | |
| Eagerly in migration for every existing agent + deferred trigger for future agents | Migration creates the sentinel for every row in agents at the time it runs. | |

**User's choice:** In the upgrade migration for the legacy agent; on registration for new agents (Recommended)

### Q4: How do we ensure the LIVE sentinel is reused, not duplicated, on re-runs?

| Option | Description | Selected |
|--------|-------------|----------|
| Partial unique index on `(agent_id) WHERE status = 'LIVE'` | Postgres enforces 'at most one LIVE batch per agent' at the DB level. | ✓ |
| App-level SELECT-before-INSERT, no DB constraint | Python code checks existence before insert. Races under concurrent registration. | |
| Composite natural key (agent_id, 'LIVE') with a unique constraint | Same effect as the partial index, slightly different shape. | |

**User's choice:** Partial unique index (Recommended)
**Notes:** Index name: `uq_scan_batches_agent_id_live`.

---

## Migration shape, indexes & rollback

### Q1: What does 'two-step migration' mean in Alembic terms?

| Option | Description | Selected |
|--------|-------------|----------|
| Two separate revisions: 012 (additive + backfill) and 013 (NOT NULL + swap unique) | Each step independently revertable; operator can pause between them. | ✓ |
| Single revision with two op blocks | One revision does both steps in sequence. Smaller PR but coarser rollback semantics. | |
| Three revisions: schema-only / backfill / tighten | Maximally granular. Probably overkill for 200K rows on a home server. | |

**User's choice:** Two separate revisions (Recommended)

### Q2: How is the backfill written inside the migration?

| Option | Description | Selected |
|--------|-------------|----------|
| Pure SQL via `op.execute` | Single UPDATE per table. Atomic, no Python imports, no model-version coupling. | ✓ |
| Python loop reading models, batched commits | More flexible but slower and couples migration to current model code. | |
| Hybrid: SQL for UPDATEs, Python for legacy agent + sentinel INSERTs | Pragmatic. | |

**User's choice:** Pure SQL via `op.execute` (Recommended)

### Q3: Which indexes should the new schema add (beyond the unique constraint swap)?

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal: drop ix on `(original_path)`, create composite unique on `(agent_id, original_path)`, add `ix_scan_batches_agent_id` | Postgres uses leading column of composite for `agent_id` filters. No separate `ix_files_agent_id` needed. | ✓ |
| Add `ix_files_agent_id` and `ix_scan_batches_agent_id` explicitly | More indexes = faster group-by-agent dispatch queries but each costs disk + write speed. | |
| You decide | Pick based on Phase 28 / Phase 27 query patterns. | |

**User's choice:** Minimal indexes (Recommended)

### Q4: How does downgrade handle the case where duplicates now exist?

| Option | Description | Selected |
|--------|-------------|----------|
| Downgrade fails loudly if dupes exist; operator must resolve manually | Better to fail than silently drop data for an irreplaceable collection. | ✓ |
| Downgrade keeps only legacy-agent rows and deletes duplicates | Dangerous — silently deletes file records. | |
| Downgrade is documented as one-way after backfill | Simplest but loses the v3.0 schema return path. | |

**User's choice:** Downgrade fails loudly (Recommended)

---

## Claude's Discretion

- Exact regex shape for the CHECK constraint on `agent_id` columns
- Whether to express the CHECK constraint in SQLAlchemy model `__table_args__`, in the Alembic migration, or both
- New `Agent` SQLAlchemy model file layout and reverse-relationship declarations
- Pydantic schemas for agent types (likely deferred to Phase 25)
- Test fixture shape for "DB with legacy agent + sentinel pre-seeded"
- Whether to add `ScanStatus.LIVE` to the model alongside the DB constraint in Phase 24 (recommended yes)
- Logging format / verbosity of the backfill step

## Deferred Ideas

- Agent self-registration / multi-tenant onboarding (OPS-06 — Future Requirements)
- mTLS in addition to bearer tokens (OPS-05)
- Cross-file-server fingerprint matching (XAGENT-01)
- Watcher catch-up / delete / move detection (WATCH-05/06/07)
- Agent metrics scraping endpoint (OPS-07)
- Reverse `Agent.files` / `Agent.scan_batches` relationships (planner discretion)
- Per-agent `scan_path` validation against `agents.scan_roots` (Phase 27 concern)
