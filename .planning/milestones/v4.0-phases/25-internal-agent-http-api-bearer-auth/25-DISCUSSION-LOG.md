# Phase 25: Internal Agent HTTP API & Bearer Auth - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-11
**Phase:** 25-internal-agent-http-api-bearer-auth
**Areas discussed:** Token format & per-request verification, Endpoint layout & router shape, Idempotency contract per endpoint, Agent body-field policy & heartbeat payload

---

## Token format & per-request verification

### Q1 — Token hash & verification scheme

| Option | Description | Selected |
|--------|-------------|----------|
| SHA-256 hex, indexed lookup | Store sha256(token) hex in agents.token_hash. Per-request: hash the bearer, single indexed SELECT, filter `revoked_at IS NULL`. Instant revocation. Tokens are full-entropy random bytes so a KDF buys nothing. | ✓ |
| Argon2id (per-agent salt) | Random per-row salt prevents indexed lookup; would require O(N) verify per request. Overkill for high-entropy tokens. | |
| KID + secret split | `<kid>.<secret>` form; lookup by kid, then constant-time compare. Supports rotation overlap. AUTH-04 only requires rotatable, not overlap-rotatable. | |

**User's choice:** SHA-256 hex, indexed lookup
**Notes:** Aligns with single-user-app scale, AUTH-04 immediate revocation, and Phase 24's String(128) token_hash column.

---

### Q2 — Token format on the wire

| Option | Description | Selected |
|--------|-------------|----------|
| Prefixed `phaze_agent_<32b>` | ~42 chars total. Prefix is grep-able in logs/code/git leaks; future GitHub secret-scanner rules can match. Server hashes the full prefix+secret string. | ✓ |
| Plain urlsafe-base64 | ~43 chars of random data. No prefix, no scanner hook. | |
| UUIDv4 string | ~36 chars, 122 bits entropy. Familiar format but no scanner hook. | |

**User's choice:** Prefixed `phaze_agent_<32b>`
**Notes:** Locks D-01 token format; server hashes the entire wire string per D-02.

---

### Q3 — Auth check wiring

| Option | Description | Selected |
|--------|-------------|----------|
| FastAPI dependency (explicit) | `agent: Agent = Depends(get_authenticated_agent)` on every internal-agent route. Appears in OpenAPI security spec; trivial to mock. | ✓ |
| Router-level dependency | Attach via `APIRouter(dependencies=[...])` for defense-in-depth. Two deps to keep in sync. | |
| ASGI middleware | Custom middleware checks path prefix and attaches `request.state.agent`. No per-route boilerplate but less explicit. | |

**User's choice:** FastAPI dependency (explicit)
**Notes:** Explicit per-route is the established phaze pattern (see `routers/health.py`). Per-route opt-in surface is the right boundary for a security-sensitive endpoint family.

---

## Endpoint layout & router shape

### Q4 — Router module layout

| Option | Description | Selected |
|--------|-------------|----------|
| Sub-package `routers/agent/` | `__init__.py`, `files.py`, `metadata.py`, etc., plus `auth.py`. Internal-agent surface visible as a package. Different shape from existing flat layout. | |
| Flat files with prefix | `agent_files.py`, `agent_metadata.py`, `agent_fingerprint.py`, `agent_execution.py`, `agent_heartbeat.py`, `agent_auth.py` directly in `routers/`. Matches existing convention exactly. | ✓ |
| One file `agent_internal.py` | All endpoints in one router. Mixes concerns; file grows. | |

**User's choice:** Flat files with prefix
**Notes:** Matches existing flat router convention; `agent_auth.py` is a helper (no `router = APIRouter(...)`) but lives alongside the routers it serves.

---

### Q5 — HTTP method & URL shape convention

| Option | Description | Selected |
|--------|-------------|----------|
| POST + REST nouns | POST everything except where PATCH is genuinely partial. Single verb to remember. | |
| Semantic verbs (PUT/PATCH/POST) | PUT for idempotent replace (metadata, fingerprint); POST for non-idempotent batch operations (file chunk upsert, execution-log create, heartbeat); PATCH for partial updates (execution-log status). | ✓ |

**User's choice:** Semantic verbs (PUT/PATCH/POST)
**Notes:** HTTP semantics carry meaning to callers and proxies. Per-endpoint verb table is captured in CONTEXT.md D-11.

---

## Idempotency contract per endpoint

### Q6 — ExecutionLog id ownership and create-vs-PATCH contract

| Option | Description | Selected |
|--------|-------------|----------|
| Agent-generated UUID + upsert | Agent generates UUID; POST does `ON CONFLICT (id) DO NOTHING`. PATCH updates mutable fields. Agent persists UUID in SAQ job state. | ✓ |
| Append-only: every state is a new row | Every state transition is a fresh POST keyed by `(proposal_id, operation, status)`. No PATCH. Would require schema change. | |
| Server-generated id + Idempotency-Key header | Stripe-style; server assigns id and dedupes by `Idempotency-Key` header. Extra table + cleanup job. | |

**User's choice:** Agent-generated UUID + upsert
**Notes:** Matches DIST-05's "agent-generated log UUIDs" phrasing. One row per file-operation; status transitions are PATCHes.

---

### Q7 — Replay semantics with mismatched payload

| Option | Description | Selected |
|--------|-------------|----------|
| Last-write-wins everywhere | All endpoints overwrite mutable fields on natural-key collision. Single rule across endpoints. Out-of-order retries can clobber newer state. | |
| Last-write-wins for data; monotonic for ExecutionLog status | Files/metadata/fingerprint/heartbeat: ON CONFLICT DO UPDATE. ExecutionLog: server enforces forward-only status transitions (PENDING < IN_PROGRESS < COMPLETED < FAILED, terminals lock). | ✓ |
| Strict 409 on payload mismatch | Reject any retry whose payload differs from the existing row. Breaks "retry safely" contract for legitimate state changes. | |

**User's choice:** Last-write-wins for data, monotonic for ExecutionLog status
**Notes:** Protects audit-trail integrity for the irreplaceable collection while keeping data writes simple. Backward transitions return 409 (Claude's discretion to silently ignore instead — see CONTEXT.md).

---

## Agent body-field policy & heartbeat payload

### Q8 — Pydantic request-body strictness

| Option | Description | Selected |
|--------|-------------|----------|
| `extra="forbid"` — 422 any extra field | Every internal-agent schema rejects unknown fields including accidental `agent_id`. Surfaces buggy clients and spoofing attempts immediately. | ✓ |
| `extra="ignore"` — silently strip | Forward-compat across agent/server version skew but silent on spoof attempts. | |
| Ignore + log when agent_id seen | Tolerant + observable middleware. Extra plumbing for a narrow concern. | |

**User's choice:** `extra="forbid"` — 422 any extra field
**Notes:** Spoofing attempts and client bugs are loud at the boundary. Acceptable tradeoff because agent + server ship together in this milestone.

---

### Q9 — Heartbeat payload shape

| Option | Description | Selected |
|--------|-------------|----------|
| Minimal: empty body | Just bumps `last_seen_at`. Server-side observability (queue depth) pulled separately by Phase 29 admin page. 204 No Content response. | |
| Small status payload | `{ agent_version, worker_pid, queue_depth }`. Server stamps last_seen_at + persists payload as JSONB in new `agents.last_status` column. | ✓ |
| Small payload, transient (no DB column) | Same payload but stored only in Redis with short TTL. Zero schema change but extra Redis key family. | |

**User's choice:** Small status payload
**Notes:** Adds an `agents.last_status` JSONB column via Phase 25's own migration (014). Heartbeat carries enough to detect wedged-but-pingable agents without a polling channel.

---

### Q10 — Auto-enqueue of extract_file_metadata on file upsert

| Option | Description | Selected |
|--------|-------------|----------|
| Phase 25 wires the auto-enqueue | File upsert endpoint enqueues `extract_file_metadata` onto `phaze-agent-<id>` for newly-inserted music/video rows. Mirrors `services/ingestion.py:158-165`. Matches SCAN-02 wording. | ✓ |
| Phase 25 only persists; Phase 26 wires enqueue | Tighter phase boundary; "callable end-to-end" weaker until Phase 26. | |
| Phase 25 emits a queued signal; Phase 26 consumes | Generic kick-the-pipeline job. More indirection; minor latency cost. | |

**User's choice:** Phase 25 wires the auto-enqueue
**Notes:** Phase 25 hard-codes the `phaze-agent-<id>` queue name inline; Phase 26 may refactor to a shared helper.

---

## Claude's Discretion

- Exact `secrets.token_urlsafe(32)` choice for the secret-portion generation.
- Whether to add `secrets.compare_digest` on the hex string for defense-in-depth (not required since hash space is uniform and indexed equality is sufficient).
- 409 vs silent-ignore on backward ExecutionLog status PATCH (409 recommended).
- Pydantic schema file split (one-file-per-router recommended).
- Error-response shape (stick with FastAPI defaults).
- Logging shape and verbosity for auth events.
- Test fixture naming/scope (`authenticated_client`, `seed_test_agent`).
- Whether to declare OpenAPI `securitySchemes.bearerAuth` (recommended).
- File-upsert chunk-size cap (recommend 1000 records/chunk).
- Whether `agent_metadata` upsert accepts `raw_tags` JSONB or just canonical fields (recommend mirror FileMetadata 1:1).
- Auto-enqueue concurrency: enqueue inside the upsert transaction vs after commit (recommend after commit).
- Exact partial-index name suffix for `ix_agents_token_hash_active`.

## Deferred Ideas

- Agent self-registration HTTP endpoint (OPS-06 Future) — operator pre-seeds via Phase 29 tooling.
- mTLS in addition to bearer tokens (OPS-05) — Phase 29 ships self-signed HTTPS first.
- Token-rotation overlap window — AUTH-04 satisfied by revoke + reissue; overlap deferred.
- Stripe-style `Idempotency-Key` header — natural-key idempotency suffices.
- Rate limiting / abuse controls — private LAN, trusted operator.
- ScanBatch HTTP endpoints — Phase 27's surface.
- LIVE sentinel auto-creation for new agents — Phase 29 admin tooling.
- Cross-file-server fingerprint matching (XAGENT-01).
- Heartbeat-driven queue depth as the sole source — Phase 29 may also poll Redis directly.
- Per-endpoint Prometheus metrics (OPS-07).
- SecretStr handling on agent-side token config — Phase 26's `agent_worker` startup.
