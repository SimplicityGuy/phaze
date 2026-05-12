---
phase: 25
slug: internal-agent-http-api-bearer-auth
status: verified
threats_total: 43
threats_closed: 43
threats_open: 0
asvs_level: standard
created: 2026-05-11
audited: 2026-05-11
---

# Phase 25 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan-time (43 threats across plans 25-01..25-08); this audit verifies each disposition holds in the implemented code.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| File-server agent → Application server | HTTP/JSON over private LAN. Bearer-token authenticated. Idempotent on natural keys. | Per-file scan records (sha256, path, size); tag metadata; fingerprint results; execution-log audit rows; heartbeat status (JSONB). |
| Application server → PostgreSQL | Async SQLAlchemy + asyncpg, in-cluster. | Agent rows (token_hash, last_seen_at, last_status); file/metadata/fingerprint/execution-log rows. |
| Application server → SAQ/Redis | Per-agent named queue `phaze-agent-<id>`. | Job payloads: `extract_file_metadata(file_id=...)`. |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status | Evidence |
|-----------|----------|-----------|-------------|------------|--------|----------|
| T-25-01-T | Tampering | Migration 014 | mitigate | Mirror migration 012 pattern; additive DDL only; clean downgrade. | closed | `alembic/versions/014_add_last_status_to_agents.py:23-51` — `upgrade()` runs `op.add_column` (line 27) + `op.create_index` (line 35); `downgrade()` drops both (lines 50-51). No data mutation. |
| T-25-01-I | Information Disclosure | Partial-index predicate drift | mitigate | Partial-index predicate `revoked_at IS NULL` literal matches `Agent.revoked_at.is_(None)` byte-for-byte. | closed | `alembic/versions/014_add_last_status_to_agents.py:40` (`postgresql_where=sa.text("revoked_at IS NULL")`) ↔ `src/phaze/routers/agent_auth.py:80` (`Agent.revoked_at.is_(None)`). |
| T-25-01-S | Spoofing | Test fixture token generation | mitigate | `secrets.token_urlsafe(32)` only; no `random` import in fixtures. | closed | `tests/conftest.py:5,76` — `import secrets`, `raw_token = "phaze_agent_" + secrets.token_urlsafe(32)`. `grep "import random"` in `tests/conftest.py` and `src/phaze/routers/agent_auth.py` returns zero matches. |
| T-25-01-E | Elevation | `seed_test_agent` reuse in production | accept | Test-only fixture; production seeding deferred to OPS-06. See Accepted Risks Log RID-25-01. | closed | `tests/conftest.py:69-87` — fixture scope is `pytest_asyncio.fixture`, lives under `tests/`. |
| T-25-02-S | Spoofing | SHA-256 brute-force | accept | Secret entropy = 32 random bytes ≈ 2^256; SHA-256 over uniform-random input is sufficient. See Accepted Risks Log RID-25-02. | closed | `src/phaze/routers/agent_auth.py:52-59` (`hash_token`) — SHA-256 over full wire string per D-02/D-04. |
| T-25-02-I-1 | Information Disclosure | 401-vs-403 enumeration oracle | mitigate | 401 only for missing/malformed header; 403 indistinguishable for unknown/revoked. | closed | `src/phaze/routers/agent_auth.py:62-84` — `HTTPBearer(auto_error=True)` raises 401 BEFORE `get_authenticated_agent` runs; line 83 raises `HTTPException(403, "Forbidden")` for both unknown and revoked. Tests `test_missing_header_returns_401`, `test_malformed_header_returns_401`, `test_unknown_token_returns_403` (`tests/test_routers/test_agent_auth.py:44,54,63`). |
| T-25-02-I-2 | Information Disclosure | Auth dep logs token | mitigate | `credentials.credentials` only used on line 76 to compute hash; no `logger`/`log`/`print` in module. | closed | `grep "logger\|log\.\|print"` on `src/phaze/routers/agent_auth.py` returns zero matches. `credentials.credentials` appears once (line 76, hashing only). |
| T-25-02-T-1 | Tampering | Caching defeats revocation | mitigate | Module docstring forbids caching; per-request fresh SELECT; `test_revoke_blocks_next_call` is the regression guard. | closed | `src/phaze/routers/agent_auth.py:24-26` (docstring forbids cache); `tests/test_routers/test_agent_auth.py:76` (`test_revoke_blocks_next_call`). |
| T-25-02-T-2 | Tampering | `== None` would break partial index | mitigate | `.is_(None)` not `== None`; lint rule E711; `test_revoke_blocks_next_call` enforces. | closed | `src/phaze/routers/agent_auth.py:80` — `Agent.revoked_at.is_(None)`. Ruff E711 active per project ruff config. |
| T-25-02-D | DoS | Brute-force / rate flooding | accept | Private LAN; rate limiting deferred per CONTEXT.md (Phase 29 hardening). See Accepted Risks Log RID-25-03. | closed | Documented deferral per `25-CONTEXT.md` "Rate limiting / abuse controls". |
| T-25-03-S | Spoofing | `agent_id` forged in file-upsert body | mitigate | `FileUpsertRecord` omits `agent_id`; `extra="forbid"` on nested + top-level (D-16); `test_agent_id_in_body_rejected`. | closed | `src/phaze/schemas/agent_files.py:25-35` (no `agent_id`), `:28,41` (`extra="forbid"` on both `FileUpsertRecord` and `FileUpsertChunk`); `src/phaze/routers/agent_files.py:62` (`data["agent_id"] = agent.id` stamped from auth dep). Test `tests/test_routers/test_agent_files.py:158` (`test_agent_id_in_body_rejected`). |
| T-25-03-T-1 | Tampering | Same-chunk duplicate paths cause Postgres "cannot affect row twice" | mitigate | Server-side dedup `dict[str, dict]` on `original_path`; last-write-wins; `test_same_chunk_duplicate_paths_dedup`. | closed | `src/phaze/routers/agent_files.py:69-72` — `deduped: dict[str, dict[str, Any]] = {}; deduped[rec["original_path"]] = rec`. Test `tests/test_routers/test_agent_files.py:188`. |
| T-25-03-T-2 | Tampering | Unicode-normalization mismatch corrupts natural key | mitigate | `unicodedata.normalize("NFC", original_path)` defensively before UPSERT. | closed | `src/phaze/routers/agent_files.py:61` — `data["original_path"] = unicodedata.normalize("NFC", data["original_path"])`. |
| T-25-03-T-3 | Tampering | `xmax` heuristic misclassifies INSERT vs UPDATE | mitigate | `RETURNING (xmax = 0) AS inserted`; regression test asserts both INSERT and UPDATE paths. | closed | `src/phaze/routers/agent_files.py:89` — `literal_column("(xmax = 0)").label("inserted")`. Test `tests/test_services/test_agent_upsert.py:28` (`test_xmax_inserted_flag`) asserts both branches. |
| T-25-03-D-1 | DoS | Unbounded chunk size | mitigate | `Field(max_length=_CHUNK_MAX)` (= 1000) on `FileUpsertChunk.files`; `test_chunk_cap_exceeded_422`. | closed | `src/phaze/schemas/agent_files.py:43` — `files: list[FileUpsertRecord] = Field(min_length=1, max_length=_CHUNK_MAX)`; `_CHUNK_MAX = settings.agent_file_chunk_max = 1000` (`src/phaze/config.py:60`). Test `tests/test_routers/test_agent_files.py:170`. |
| T-25-03-I | Information Disclosure | Sensitive logs (bearer / full path) | mitigate | Logger only emits `row.id` + `agent.id`; never raw bearer; never full path. | closed | `src/phaze/routers/agent_files.py:114` — `logger.exception("...file_id=%s agent_id=%s", row.id, agent.id)`. No bearer or path strings in any log call. |
| T-25-03-D-2 | DoS | Queue connection leak | mitigate | `try/finally: await queue.disconnect()`; test asserts `disconnect.assert_awaited_once()`. | closed | `src/phaze/routers/agent_files.py:101,115-116` — `try: ... finally: await queue.disconnect()`. Test `tests/test_routers/test_agent_files.py:123`. |
| T-25-03-E | Elevation | Pre-commit enqueue could leak state on rollback | mitigate | Enqueue strictly AFTER `session.commit()`; on enqueue failure, log + continue. | closed | `src/phaze/routers/agent_files.py:91-93` (`session.execute` then `await session.commit()`) → lines 98-114 (enqueue loop with `except Exception: logger.exception(...)`). |
| T-25-04-S | Spoofing | `agent_id` forged in metadata/fingerprint/heartbeat | mitigate | Every request schema declares `extra="forbid"`, omits `agent_id`; auth dep stamps `agent.id`; `test_metadata_extra_field_422`. | closed | `src/phaze/schemas/agent_metadata.py:15`, `agent_fingerprint.py:11`, `agent_heartbeat.py:12`, `agent_execution.py:29,51` all set `ConfigDict(extra="forbid")` and omit `agent_id`. Test `tests/test_routers/test_agent_metadata.py:124` (`test_metadata_extra_field_422`); also `tests/test_routers/test_agent_files.py:148` (`test_extra_body_field_422`); also `tests/test_routers/test_agent_execution.py:260`. |
| T-25-04-T-1 | Tampering | Cross-agent file_id writes (no authorization) | accept | Cross-agent file_id authorization out of scope for Phase 25; deferred to Phase 29. See Accepted Risks Log RID-25-04. | closed | Documented in `25-VERIFICATION.md` Anti-Patterns ("No verification that `file_id`/`proposal_id` belongs to the authenticated agent — accepted as T-25-04-T deferred to Phase 29"). |
| T-25-04-I | Information Disclosure | Extra heartbeat fields | mitigate | `HeartbeatRequest` declares exactly three required fields; `extra="forbid"` rejects accidents. | closed | `src/phaze/schemas/agent_heartbeat.py:6-16` — `agent_version`, `worker_pid`, `queue_depth` only, `ConfigDict(extra="forbid")`. Test `tests/test_routers/test_agent_heartbeat.py:71`. |
| T-25-04-T-2 | Tampering | Oversized JSON body | accept | FastAPI/Starlette default body-size limit absorbs oversized JSON. See Accepted Risks Log RID-25-05. | closed | Documented deferral per plan threat model. |
| T-25-04-D | DoS | Concurrent heartbeats overwhelm DB | accept | Bounded by Postgres connection pool; rate limiting deferred. See Accepted Risks Log RID-25-06. | closed | Documented deferral per plan threat model. |
| T-25-04-E | Elevation | Stale agent reuse after revoke | mitigate | `get_authenticated_agent` does a fresh `SELECT` per request; `test_heartbeat_revoke_blocks_next_call`. | closed | `src/phaze/routers/agent_auth.py:80-83` — fresh SELECT every request, no cache. Test `tests/test_routers/test_agent_heartbeat.py:86`. |
| T-25-05-T-1 | Tampering | ExecutionLog POST PK violation on retry | mitigate | `INSERT ... ON CONFLICT (id) DO NOTHING`; `test_create_replay_no_op`. | closed | `src/phaze/routers/agent_execution.py:77` — `.on_conflict_do_nothing(index_elements=["id"])`. Test `tests/test_routers/test_agent_execution.py:153`. |
| T-25-05-T-2 | Tampering | Status regression breaks audit trail | mitigate | D-15 monotonic ladder via `_STATUS_ORDER`; `_STATUS_ORDER[new] < _STATUS_ORDER[cur]` → 409. | closed | `src/phaze/routers/agent_execution.py:51-56,121-122` — `_STATUS_ORDER` ladder + strict `<` check returning 409 `"would regress"`. Test `tests/test_routers/test_agent_execution.py:175` (`test_monotonic_regress_returns_409`). |
| T-25-05-R | Repudiation | Terminal row mutation hides history | mitigate | Terminal-state guard returns 409 before regress check; `test_terminal_state_rejects_patch`. | closed | `src/phaze/routers/agent_execution.py:117-118` — `if cur in _TERMINAL and new != cur: raise HTTPException(409, "execution-log status is terminal")`. Test `tests/test_routers/test_agent_execution.py:200`. |
| T-25-05-S | Spoofing | `agent_id` injected via ExecutionLogCreate | mitigate | `ExecutionLogCreate` omits `agent_id`; `extra="forbid"`; agent.id from auth dep into response only. | closed | `src/phaze/schemas/agent_execution.py:20-38` — no `agent_id` field, `ConfigDict(extra="forbid")`; `src/phaze/routers/agent_execution.py:80` (`agent_id=agent.id` from auth dep). Test `tests/test_routers/test_agent_execution.py:260` (`test_extra_body_field_422`). |
| T-25-05-I | Information Disclosure | Verbose error envelope leaks internals | mitigate | FastAPI default `{"detail": "..."}` envelope; `settings.debug=False` in production. | closed | `src/phaze/routers/agent_execution.py:106,118,122` use `HTTPException(detail="...")` — default FastAPI envelope. `src/phaze/config.py:19` (`debug: bool = False`). |
| T-25-05-T-3 | Tampering | Same-status retry causes 409 (idempotency break) | mitigate | Strict `<` comparator (NOT `<=`); `test_same_status_patch_allowed`. | closed | `src/phaze/routers/agent_execution.py:121` — `_STATUS_ORDER[new] < _STATUS_ORDER[cur]` (strict `<`). Test `tests/test_routers/test_agent_execution.py:225`. |
| T-25-05-T-4 | Tampering | Unknown id silently coerced into 500 | mitigate | `session.get(ExecutionLog, id)` first; 404 if None; `test_patch_unknown_id_returns_404`. | closed | `src/phaze/routers/agent_execution.py:104-106` — `existing = await session.get(ExecutionLog, execution_log_id); if existing is None: raise HTTPException(404, ...)`. Test `tests/test_routers/test_agent_execution.py:249`. |
| T-25-06-T-1 | Tampering | `main.py` accidentally imports/uses `agent_auth` as a router | mitigate | `main.py` omits `agent_auth` import; `grep -c "agent_auth" main.py == 0`. | closed | Verified `grep "agent_auth" src/phaze/main.py` returns no matches. Auth dep is imported only by the 5 agent routers (`src/phaze/routers/agent_files.py:30`, `agent_metadata.py:13`, `agent_fingerprint.py:13`, `agent_execution.py:35`, `agent_heartbeat.py:12`). |
| T-25-06-T-2 | Tampering | Chunk-cap misconfiguration | accept | `agent_file_chunk_max` is operator-controlled via env. See Accepted Risks Log RID-25-07. | closed | `src/phaze/config.py:60` — env-driven setting. |
| T-25-06-I | Information Disclosure | OpenAPI exposes `bearerAuth` security scheme | accept | Required for /docs lock-icon rendering. See Accepted Risks Log RID-25-08. | closed | `src/phaze/routers/agent_auth.py:40-43` — `HTTPBearer(scheme_name="bearerAuth", ...)`; verified by `test_openapi_bearer_scheme` (`tests/test_routers/test_agent_auth.py:134`). |
| T-25-06-E | Elevation | Internal routes overlap with operator routes | mitigate | Explicit `/api/internal/agent/<resource>` prefix per D-10; non-overlapping with operator routes. | closed | Prefixes: `src/phaze/routers/agent_files.py:36` (`/api/internal/agent/files`), `agent_metadata.py:17`, `agent_fingerprint.py:17`, `agent_execution.py:44`, `agent_heartbeat.py:16`. Operator routes (scan/proposals/execution/etc.) use distinct prefixes. |
| T-25-06-D | DoS | `chunk_max=0` misconfiguration | accept | Operator-side; `ge=1` validator is a follow-up. See Accepted Risks Log RID-25-09. | closed | Documented deferral per plan threat model. |
| T-25-07-T | Tampering | Partial-PUT clobbers unset metadata fields with NULL (CR-01) | mitigate | `body.model_dump(exclude_unset=True)`; UPDATE SET clause derived from `dumped` keys only; `test_metadata_partial_put_preserves_other_fields`. | closed | `src/phaze/routers/agent_metadata.py:52,63,68` — `dumped = body.model_dump(exclude_unset=True)`; `set_={k: stmt.excluded[k] for k in dumped}`; `on_conflict_do_nothing` fallback for empty body. Test `tests/test_routers/test_agent_metadata.py:148` (`test_metadata_partial_put_preserves_other_fields`); `tests/test_routers/test_agent_metadata.py:193` (`test_metadata_empty_put_is_noop_for_existing_row`). |
| T-25-07-R | Repudiation | Agent-id attribution unchanged by partial-PUT fix | accept | `agent_id` resolved from auth dep unchanged; pre-existing `test_metadata_replay_overwrites` covers attribution. See Accepted Risks Log RID-25-10. | closed | `src/phaze/routers/agent_metadata.py:71` — response `agent_id=agent.id` from auth dep. Test `tests/test_routers/test_agent_metadata.py:99`. |
| T-25-07-I | Information Disclosure | New disclosure vector via CR-01 fix | accept | No new disclosure surface; only UPDATE semantics changed. See Accepted Risks Log RID-25-11. | closed | Diff scoped to `model_dump(exclude_unset=True)` + `on_conflict_do_nothing` fallback; no new response fields, no new logging. |
| T-25-08-D | DoS | Retry storms on ambiguous PATCH contract | mitigate | CR-02 carve-out removes contract ambiguity (same-status terminal retry → 200); `test_same_status_patch_terminal_allowed`. | closed | `src/phaze/routers/agent_execution.py:117` — `if cur in _TERMINAL and new != cur:`. Test `tests/test_routers/test_agent_execution.py:278` (`test_same_status_patch_terminal_allowed`). |
| T-25-08-T | Tampering | Over-broad CR-02 carve-out allows terminal→other-terminal | mitigate | Narrow carve-out `new != cur`; `test_terminal_completed_to_failed_still_rejected`. | closed | `src/phaze/routers/agent_execution.py:117` — operator `and new != cur` ensures only same-status retries pass. Test `tests/test_routers/test_agent_execution.py:351` (`test_terminal_completed_to_failed_still_rejected` — asserts 409 for COMPLETED→FAILED). |
| T-25-08-R | Repudiation | Agent-id attribution unchanged by CR-02 fix | accept | `agent_id` resolved from auth dep unchanged; CR-02 only narrowed the terminal guard. See Accepted Risks Log RID-25-12. | closed | `src/phaze/routers/agent_execution.py:80,130` — `agent_id=agent.id` from auth dep, unchanged. |
| T-25-08-I | Information Disclosure | New error-string leak via CR-02 fix | accept | Error strings unchanged ("execution-log status is terminal" / "would regress"). See Accepted Risks Log RID-25-13. | closed | `src/phaze/routers/agent_execution.py:118,122` — identical detail strings; verified in tests `test_terminal_state_rejects_patch`, `test_monotonic_regress_returns_409`, `test_terminal_completed_to_failed_still_rejected`. |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| RID-25-01 | T-25-01-E | `seed_test_agent` is a `tests/conftest.py` fixture only — not exported, not imported by production code, never persisted outside the test transaction. Production agent seeding is operator-side and tracked under OPS-06 (deferred to Phase 29). | Phase 25 plan-time | 2026-05-11 |
| RID-25-02 | T-25-02-S | Bearer token is 32 random bytes (`secrets.token_urlsafe(32)`, ≈256 bits of entropy). SHA-256 brute-force on a uniformly-random preimage requires 2^256 operations — computationally infeasible. A KDF would break the indexed-equality-lookup model (D-04) without measurable security improvement. | Phase 25 plan-time | 2026-05-11 |
| RID-25-03 | T-25-02-D | Phaze v4.0 runs on a private LAN with a single trusted operator and a small known set of agents. No `slowapi`/`fastapi-limiter` rate limiting in Phase 25; revisited if multi-tenant arrives (OPS-06 follow-on; documented in `25-CONTEXT.md` Deferred Ideas). | Phase 25 plan-time | 2026-05-11 |
| RID-25-04 | T-25-04-T-1 | Cross-agent `file_id`/`proposal_id` authorization is explicitly out of scope for Phase 25. The natural-key idempotency contract assumes a single trusted operator; Phase 29 will add per-agent authorization checks for the Agents admin surface. Tracked under WR-01 in `25-REVIEW.md`. | Phase 25 plan-time | 2026-05-11 |
| RID-25-05 | T-25-04-T-2 | FastAPI/Starlette default request-body size limit (~1 MB) absorbs oversized JSON. Per-route body-size limits are deferred — Phase 25 chunk-cap (1000 records) bounds the meaningful payload. | Phase 25 plan-time | 2026-05-11 |
| RID-25-06 | T-25-04-D | Heartbeat concurrency is bounded by the Postgres connection pool (default 10 connections). Per-agent rate limiting deferred to Phase 29 hardening; in v4.0 a single agent cannot overwhelm the database. | Phase 25 plan-time | 2026-05-11 |
| RID-25-07 | T-25-06-T-2 | `agent_file_chunk_max` defaults to 1000 in `config.py:60` and is env-overridable. An operator-set value below 1 would be a misconfiguration caught by Pydantic validation when added — out of scope for Phase 25 (follow-up: add `ge=1` validator). | Phase 25 plan-time | 2026-05-11 |
| RID-25-08 | T-25-06-I | The `bearerAuth` OpenAPI security scheme is REQUIRED for /docs to render the lock icon. Internal-agent routes are only reachable on the private LAN; exposing the scheme name is not a credential leak. | Phase 25 plan-time | 2026-05-11 |
| RID-25-09 | T-25-06-D | A misconfigured `chunk_max=0` would block all upserts (422 on every chunk). Operator-side configuration concern; `ge=1` follow-up tracked. | Phase 25 plan-time | 2026-05-11 |
| RID-25-10 | T-25-07-R | The CR-01 fix (Plan 25-07) only altered metadata UPDATE semantics. The `agent_id` derivation path (auth dep → response) and pre-existing `test_metadata_replay_overwrites` regression remain intact. | Phase 25 plan-time | 2026-05-11 |
| RID-25-11 | T-25-07-I | CR-01 diff is scoped to a single `model_dump(exclude_unset=True)` change plus an `on_conflict_do_nothing` fallback for empty bodies. No new response fields, no new log lines — zero new disclosure surface. | Phase 25 plan-time | 2026-05-11 |
| RID-25-12 | T-25-08-R | CR-02 (Plan 25-08) narrowed the terminal-state guard to `and new != cur`. The `agent_id` derivation path is unaffected. | Phase 25 plan-time | 2026-05-11 |
| RID-25-13 | T-25-08-I | CR-02 fix preserved the existing detail strings byte-for-byte (`"execution-log status is terminal"`, `"execution-log status would regress"`). Verified by `test_terminal_state_rejects_patch`, `test_monotonic_regress_returns_409`, and `test_terminal_completed_to_failed_still_rejected`. | Phase 25 plan-time | 2026-05-11 |

*Accepted risks do not resurface in future audit runs.*

---

## Unregistered Flags

None. Every `## Threat Flags` entry across `25-01..25-08-SUMMARY.md` maps to an existing threat ID in this register. The `25-08-SUMMARY.md` Threat Flags section explicitly states "None. The fix narrows an existing guard..." and maps `T-25-08-D` / `T-25-08-T` to passing regression tests.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-05-11 | 43 | 43 | 0 | Claude (gsd-secure-phase) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log (13 entries, RID-25-01..RID-25-13)
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-05-11
