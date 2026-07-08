---
phase: 77
slug: additive-schema-rescan-wipe-fix-migration-032
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-08
---

# Phase 77 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> **Audit disposition:** SECURED — all declared threat mitigations verified present in merged code (commit `faee8b8a`).
> Verification is evidence-based (grep/read of the implementation), not documentation-based. Implementation files were treated as read-only.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| file-server agent → agent upsert endpoint | Agent-authenticated HTTP crosses here (`routers/agent_files.py` `upsert_files`). `original_path`/`file_size`/`sha256_hash` are agent-supplied; `agent_id` is server-derived from the auth dependency. | File paths, sizes, sha256 hashes (non-PII); server-stamped agent identity |
| migration author → live Postgres corpus | Migration `032` DDL/DML runs with schema-owner privilege against the ~200K-file corpus; the only "input" is static SQL literals authored in-file (no runtime/user input). | Static SQL literals only |
| Alembic migration ↔ SAQ-owned `saq_jobs` | `saq_jobs` is owned by SAQ (init_db + saq_versions). An Alembic migration touching it would collide with SAQ's own schema management. | (must not cross — enforced) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-77-01 | Tampering | `alembic/versions/032_add_derived_status_schema.py` backfills | mitigate | All 5 backfills are module-level static string literals via `op.execute(sa.text(...))` (`032:71-118`, `:160-164`) — no f-string/interpolation, no model import, no user input. `FileState` values are fixed literals. bandit S608 clean. | closed |
| T-77-02 | Tampering/Repudiation | migration `032` referencing `saq_jobs` | mitigate | CRITICAL "NEVER reference saq_jobs" banner at docstring `032:39-41` + `test_migration_never_references_saq_jobs` grep guard (`test_migration_032_additive_schema.py:99-103`). `saq_jobs` appears only in banner/comment lines; migration touches only analysis/metadata/dedup_resolution/cloud_job. | closed |
| T-77-03 | Tampering | `routers/agent_files.py` `upsert_files` | mitigate | `agent_files.py:110` stamps `data["agent_id"] = agent.id` from `Depends(get_authenticated_agent)` (`:61`), NEVER from the request body (AUTH-01). Phase 77 change removed only the `state` key from the ON CONFLICT `set_` dict (`:129-139`); auth path unchanged. | closed |
| T-77-04 | Elevation/Repudiation | rescan overwriting an already-advanced file's state | mitigate | `state` key removed from ON CONFLICT `set_` at BOTH sites: `services/ingestion.py:111-120` and `routers/agent_files.py:129-139` (`grep "state.*excluded"` → NONE). Conflict target stays composite `(agent_id, original_path)`. Non-vacuous regression tests: `tests/discovery/test_rescan_preserves_state.py:95,98` and `tests/agents/test_rescan_preserves_state.py:124,129,132`. | closed |
| T-77-05 | Tampering | `models/cloud_job.py` `status_enum` CHECK membership | mitigate | DB CHECK is the authoritative gate, listing all 7 members incl `'awaiting'` in BOTH the model (`cloud_job.py:114`) and migration (`032:69,146` via `create_check_constraint`). Enum member `CloudJobStatus.AWAITING` (`cloud_job.py:50`) cannot bypass the CHECK. | closed |
| T-77-06 | Information disclosure | `models/dedup_resolution.py` FKs to `files.id` | accept | Both FK columns reference internal `files.id` UUIDs only: `dedup_resolution.py:35` (`file_id`), `:39` (`canonical_file_id`, nullable). No endpoint/serializer exposes the table (`grep` over `routers`/`schemas` → NONE). See Accepted Risks Log. | closed |
| T-77-07 | Tampering | migration `032` backfills writing `files.state` | mitigate | `files.state` is READ-ONLY — appears only in `WHERE f.state = '...'` SELECT predicates (`032:78,92,100,108,116`); no `UPDATE files`/`SET state` in the migration. Integration test snapshots and asserts byte-unchanged (`test_migration_032_additive_schema.py:178`, `:233-234`). | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-77-01 | T-77-06 — Information disclosure, `dedup_resolution` FKs to `files.id` | Both FK columns (`file_id`, `canonical_file_id`) reference internal file UUIDs only — no PII, no external identifiers. `canonical_file_id` is nullable/best-effort (RESEARCH Pitfall 4). Phase 77 is additive ORM schema + backfill only; nothing reads or serializes the table yet (grep over routers/schemas → NONE). **Boundary:** acceptance is scoped to Phase 77. A future "duplicate of X" UI that serializes `dedup_resolution` must re-evaluate exposure of `canonical_file_id`. | Robert Wlodarczyk | 2026-07-08 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-08 | 7 | 7 | 0 | gsd-security-auditor (opus) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-08
