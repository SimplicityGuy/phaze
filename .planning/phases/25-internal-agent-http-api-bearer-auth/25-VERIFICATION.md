---
phase: 25-internal-agent-http-api-bearer-auth
verified: 2026-05-11T00:00:00Z
status: gaps_found
score: 3/5 must-haves verified
overrides_applied: 0
gaps:
  - truth: "Replaying the same chunk of file upserts, the same proposal mutation, or the same execution-log PATCH with the same natural keys (`(agent_id, original_path)`, `file_id`, `proposal_id`, agent-generated log UUIDs) produces no duplicate rows and the same final state"
    status: failed
    reason: "Two idempotency contracts are broken at the production HTTP boundary. Both bugs reproduced end-to-end against the wired routers in src/phaze/main.py."
    artifacts:
      - path: "src/phaze/routers/agent_execution.py"
        issue: "Lines 107-122: the terminal-state guard runs BEFORE any same-status carve-out. `PATCH /execution-log/{id} {\"status\": \"completed\"}` against a row already in COMPLETED returns 409 `execution-log status is terminal` instead of 200. This is the canonical idempotent-retry case (network blip after the agent wrote COMPLETED, SAQ retries the job, agent re-sends the same PATCH). End-to-end reproduction against the production router returned `409 {'detail': 'execution-log status is terminal'}` for an exact-replay COMPLETED→COMPLETED PATCH."
      - path: "src/phaze/routers/agent_metadata.py"
        issue: "Lines 41-50: `body.model_dump()` is called without `exclude_unset=True`. Combined with Pydantic schema `MetadataWriteRequest` (all 9 fields `Optional[...] = None`), every PUT writes ALL 9 columns to the SET clause regardless of which fields the client sent. End-to-end reproduction: PUT `{artist:'Aphex Twin', title:'Xtal', year:1992, album:'SAW85-92'}` then PUT `{artist:'Aphex Twin v2'}` against the same file_id produces a row with `artist='Aphex Twin v2'` but `title=None, year=None, album=None` — destructive overwrite. Idempotent replay is therefore only safe if the agent sends the same fields every time; partial-replay corrupts data."
    missing:
      - "agent_execution.py: change the terminal-state guard to allow same-status PATCH: `if cur in _TERMINAL and new != cur: raise 409 'execution-log status is terminal'`. Add regression test `test_same_status_patch_terminal_allowed` that POSTs a COMPLETED row, PATCHes with `{\"status\": \"completed\"}`, and asserts 200. Currently `test_same_status_patch_allowed` only covers IN_PROGRESS→IN_PROGRESS and `test_terminal_state_rejects_patch` only covers COMPLETED→IN_PROGRESS (regression), so the actual canonical retry case is untested."
      - "agent_metadata.py: change to `dumped = body.model_dump(exclude_unset=True)` and derive `update_keys = set(dumped.keys())`, using `dumped` for both the INSERT payload (with defaults stamped where pg_insert needs them) and the SET clause. Add regression test `test_metadata_partial_put_preserves_other_fields` that PUTs full metadata then PUTs `{'artist': 'X'}` and asserts the existing `title`/`year`/`album` survive. The current `test_metadata_replay_overwrites` sends BOTH fields on both replays, so it never exercises the partial-PUT path."
human_verification:
  - test: "After fixing the two idempotency bugs, exercise the full end-to-end agent retry flow with a real httpx client against the production app to confirm: (a) network glitch after COMPLETED PATCH → retry returns 200; (b) partial metadata PUT preserves prior fields; (c) same chunk of file upserts replayed produces no duplicates (already works — `test_replay_no_duplicates` passes)."
    expected: "All three replay scenarios return success and preserve final state."
    why_human: "Real-world retry behavior under network failure is the operational concern Success Criterion #3 was written to address. Unit tests have been shown to miss both bugs because they only test the happy paths the implementer thought to write. A human-driven scenario walk validates the contract from the agent's perspective."
  - test: "Verify the production app's OpenAPI lock icon appears on every /api/internal/agent/* route in /docs (Swagger UI)."
    expected: "All 6 routes show the lock icon and 401 responses match the bearer-scheme."
    why_human: "OpenAPI UI rendering is visual."
---

# Phase 25: Internal Agent HTTP API & Bearer Auth Verification Report

**Phase Goal:** The application server exposes an authenticated, idempotent HTTP surface that agents can call to record every state change, with `agent_id` derived from the bearer token and never trusted from request bodies.

**Verified:** 2026-05-11
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

The phase ships the surface area required by the goal — five routers wired into `create_app()`, bearer-token auth, OpenAPI scheme — but the **idempotency contract that defines this phase's goal is broken at two of the five endpoints.** The implementation is well-organized and most of the surface is correct, but two BLOCKER bugs in idempotent-replay semantics (verified end-to-end against the production routers) mean Success Criterion #3 — the contract that gives the phase its reason for existing — is FAILED.

### Observable Truths (Roadmap Success Criteria)

| # | Truth (ROADMAP success criterion) | Status | Evidence |
|---|---|---|---|
| 1 | Every `/api/internal/agent/*` route requires a bearer token; an unauthenticated request returns 401 and an unknown/revoked token returns 403 | ✓ VERIFIED | `src/phaze/routers/agent_auth.py:62-84` — `HTTPBearer(auto_error=True)` raises 401 with `WWW-Authenticate: Bearer` for missing/malformed; in-function `HTTPException(403, "Forbidden")` for unknown/revoked. Every router imports `get_authenticated_agent` (`grep -l` returns all 5). Tests `test_missing_header_returns_401`, `test_malformed_header_returns_401`, `test_unknown_token_returns_403` all pass. |
| 2 | The `agent_id` used by every endpoint is resolved by hashing the bearer token and looking it up in the `agents` table; any `agent_id` field in a request body is ignored or rejected | ✓ VERIFIED | All request schemas declare `model_config = ConfigDict(extra="forbid")` (verified by grep on `src/phaze/schemas/agent_*.py`). Every router stamps `agent.id` from `Depends(get_authenticated_agent)` rather than reading from the body. Test `test_metadata_extra_field_422` asserts `loc=["body", "agent_id"]` 422 envelope for forged-body attempts (mirrored across all routers). Hash function = `sha256(full_wire_string)` over the canonical token — implementation matches CONTEXT D-02. |
| 3 | Replaying the same chunk of file upserts, the same proposal mutation, or the same execution-log PATCH with the same natural keys produces no duplicate rows and the same final state | ✗ FAILED | **TWO idempotency bugs verified end-to-end against the production routers.** (a) `agent_execution.py:111-116` — terminal-state guard runs before any same-status carve-out, so `PATCH COMPLETED → COMPLETED` returns 409 instead of 200 (verified by end-to-end test against production router). (b) `agent_metadata.py:41-50` — partial PUT silently NULLs all unset columns because `body.model_dump()` is called without `exclude_unset=True` (verified: `{artist, title, year, album}` PUT followed by `{artist}` PUT produces row with only `artist` set, all other prior fields nulled). File-upsert idempotency (`test_replay_no_duplicates`) works correctly via composite UQ + `ON CONFLICT DO UPDATE`. ExecutionLog POST replay (`test_create_replay_no_op`) works via `ON CONFLICT (id) DO NOTHING`. The two failing paths sit at PATCH-against-terminal and PUT-with-partial-payload — both common in real retry scenarios. |
| 4 | Setting `agents.revoked_at` on a row immediately causes that agent's next `/api/internal/agent/*` call to be rejected with no application-server restart required | ✓ VERIFIED | `agent_auth.py:80` uses `Agent.revoked_at.is_(None)` predicate (byte-for-byte matching the partial index `ix_agents_token_hash_active WHERE revoked_at IS NULL` from migration 014). Module-level docstring explicitly forbids caching. Test `test_revoke_blocks_next_call` POSTs `update(Agent).values(revoked_at=NOW())` between two requests and asserts the second returns 403 without a `create_app()` restart. Test `test_heartbeat_revoke_blocks_next_call` reaffirms on the production heartbeat route. |
| 5 | The API surface covers file upsert, metadata write, fingerprint write, execution-log create/patch, and heartbeat — all callable end-to-end | ✓ VERIFIED | Production app (`create_app()`) registers all 5 routers (verified via `grep -c "include_router" main.py` = 17, with 5 phase-25 routers in the cohort). Runtime verification: `python -c "create_app()"` exposes 6 `/api/internal/agent/*` paths (files, metadata/{file_id}, fingerprints/{file_id}/{engine}, execution-log, execution-log/{id}, heartbeat). |

**Score:** 3/5 truths verified, 1 FAILED, 1 partially blocked by Truth 3.

**The phase goal itself contains the word "idempotent"** — Success Criterion #3 is not an incidental property, it is the contract that distinguishes this phase from "ship some HTTP routes." The bugs reproduced above mean an agent that experiences a network failure after writing COMPLETED, or that sends a partial metadata update on retry, will either receive a confusing 409 or destroy prior data. The phase **cannot proceed to Phase 26 (agent-side HTTP client)** until these are fixed, because Phase 26 will be built against an incorrect server contract.

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/phaze/routers/agent_auth.py` | Bearer auth helper | ✓ VERIFIED | 85 lines, exports `bearer_scheme`, `hash_token`, `get_authenticated_agent`. 100% test coverage. Imported by all 5 agent routers. |
| `src/phaze/routers/agent_files.py` | POST /api/internal/agent/files | ✓ VERIFIED | Composite UQ UPSERT idempotent via `ON CONFLICT DO UPDATE`. xmax-based INSERT detection. NFC normalization on `original_path`. Per-agent SAQ queue enqueue. Wired into main.py. |
| `src/phaze/routers/agent_metadata.py` | PUT /api/internal/agent/metadata/{file_id} | ⚠️ HOLLOW | Router exists, route registered, auth gating works, but the idempotent-replay contract is broken when partial payloads are sent (Bug CR-01). |
| `src/phaze/routers/agent_fingerprint.py` | PUT /api/internal/agent/fingerprints/{file_id}/{engine} | ✓ VERIFIED | Composite UQ `(file_id, engine)` UPSERT with explicit `set_={status, error_message}`. Wired in. |
| `src/phaze/routers/agent_execution.py` | POST + PATCH /api/internal/agent/execution-log | ⚠️ HOLLOW | POST replay-safety works (`ON CONFLICT (id) DO NOTHING`). PATCH same-status non-terminal works. **PATCH same-status terminal returns 409, breaking idempotent retry contract (Bug CR-02).** |
| `src/phaze/routers/agent_heartbeat.py` | POST /api/internal/agent/heartbeat → 204 | ✓ VERIFIED | Single `update(Agent)` writes `last_seen_at + last_status`. Returns 204 no body. |
| `alembic/versions/014_add_last_status_to_agents.py` | last_status JSONB + partial token-hash index | ✓ VERIFIED | Migration adds column + `ix_agents_token_hash_active (token_hash) WHERE revoked_at IS NULL`. Roundtrip downgrade/upgrade verified. Predicate matches `.is_(None)` byte-for-byte. |
| `src/phaze/main.py` | All 5 agent routers wired | ✓ VERIFIED | `grep -c include_router` returns 17, 5 of which are `agent_*` modules. `agent_auth` correctly omitted (helper, not router). |
| `src/phaze/config.py` | `agent_token_prefix` + `agent_file_chunk_max` settings | ✓ VERIFIED | Both fields present with `noqa: S105` for token-prefix. |
| `tests/conftest.py` | `seed_test_agent` + `authenticated_client` fixtures | ✓ VERIFIED | Both fixtures present, used by all phase-25 router tests. |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| All 5 agent routers | `agent_auth.get_authenticated_agent` | `Depends(get_authenticated_agent)` | ✓ WIRED | `grep -l "from phaze.routers.agent_auth import get_authenticated_agent"` returns all 5 router files. Auth dep stamps `agent.id` into router-local data, NEVER from body. |
| `agent_auth.py` SELECT | migration 014 partial index | `Agent.revoked_at.is_(None)` | ✓ WIRED | SQLAlchemy `.is_(None)` renders `WHERE revoked_at IS NULL`, byte-for-byte matching the partial-index predicate `postgresql_where=sa.text("revoked_at IS NULL")`. |
| `agent_files.py` enqueue | per-agent SAQ queue | `Queue.from_url(redis_url, name=f"phaze-agent-{agent.id}")` | ✓ WIRED | Queue construction + enqueue + try/finally disconnect verified by `test_auto_enqueue_only_for_inserts`. |
| `main.py` include_router | each phase-25 router | `app.include_router(...)` | ✓ WIRED | All 5 calls present, production runtime exposes 6 `/api/internal/agent/*` paths. |
| `agent_metadata.py` PUT | `FileMetadata` natural-key UPSERT | `pg_insert(...).on_conflict_do_update(index_elements=["file_id"], set_={...all body fields...})` | ⚠️ WIRED-BUT-INCORRECT | UPSERT idempotency at the row-count level works (1 PUT = 1 row, 2 PUTs = 1 row). **But field-level last-write-wins clobbers prior data on partial PUT — Bug CR-01.** |
| `agent_execution.py` PATCH | monotonic guard | `_STATUS_ORDER` + `_TERMINAL` frozenset | ⚠️ WIRED-BUT-INCORRECT | Lifecycle ladder enforced; backward transitions correctly 409. **But same-status PATCH against terminal returns 409 instead of 200 — Bug CR-02.** |

### Data-Flow Trace (Level 4)

This phase produces HTTP endpoints, not rendered components. Data flow is request-payload → handler → DB. Validated end-to-end against a live test DB for the two bug paths (see Behavioral Spot-Checks).

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Production app exposes 6 agent routes | `python -c "create_app()"` enumeration | All 6 `/api/internal/agent/*` paths registered | ✓ PASS |
| All phase-25 router tests pass | `uv run pytest tests/test_routers/test_agent_*.py tests/test_services/test_agent_upsert.py -q` | `33 passed in 7.62s` | ✓ PASS |
| 401 + WWW-Authenticate on missing header | `test_missing_header_returns_401` | passes | ✓ PASS |
| 403 on unknown bearer | `test_unknown_token_returns_403` | passes, body `{"detail":"Forbidden"}` byte-for-byte | ✓ PASS |
| Revoke mid-session blocks next call | `test_revoke_blocks_next_call` | passes | ✓ PASS |
| File-upsert replay produces 1 row | `test_replay_no_duplicates` | passes (1 row after 2 PUTs) | ✓ PASS |
| ExecutionLog POST replay = no-op | `test_create_replay_no_op` | passes (1 row after 2 POSTs) | ✓ PASS |
| **Metadata partial-PUT preserves prior fields** | End-to-end real-DB script: full PUT then `{artist}` partial PUT, then SELECT | **`title`, `year`, `album` all None after partial PUT — Bug CR-01 reproduced end-to-end** | ✗ FAIL |
| **ExecutionLog idempotent terminal retry returns 200** | End-to-end real-DB script: POST COMPLETED, then PATCH same `{status: completed}` | **Returns `409 {"detail":"execution-log status is terminal"}` — Bug CR-02 reproduced end-to-end against production router** | ✗ FAIL |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|---|---|---|---|---|
| AUTH-01 | 25-01, 25-02, 25-03, 25-04, 25-05 | Bearer-token auth; `agent_id` from token, never from body | ✓ SATISFIED | `agent_auth.get_authenticated_agent` is correct; `extra="forbid"` on all request schemas; routers stamp `agent.id` from auth dep. |
| AUTH-04 | 25-01, 25-02, 25-04 | Revoke immediately blocks; no restart required | ✓ SATISFIED | `.is_(None)` predicate + partial index + no in-process cache. `test_revoke_blocks_next_call` is the active regression guard. |
| DIST-04 | 25-01, 25-03, 25-04, 25-05 | Authenticated HTTPS calls cover every state change (file upsert, metadata, fingerprint, exec-log, heartbeat) | ✓ SATISFIED | All 5 surfaces exist, are auth-gated, and are wired into production app. Surface coverage matches the requirement enumeration. |
| DIST-05 | 25-01, 25-03, 25-04, 25-05 | Every endpoint is idempotent on retry; natural keys guarantee replay safety | ✗ BLOCKED | Idempotency is broken at two paths: (a) `PATCH /execution-log/{id}` same-status against terminal row returns 409 instead of 200 — direct contradiction of "replaying ... PATCH ... produces ... same final state"; (b) `PUT /metadata/{file_id}` with partial payload does NOT produce "same final state" on replay — it produces a **different and corrupted** state where unset fields get nulled. Row-count idempotency is preserved for both; field-content idempotency is not. |

No orphaned requirements: REQUIREMENTS.md maps DIST-04, DIST-05, AUTH-01, AUTH-04 all to Phase 25, and all four appear in at least one plan's `requirements-completed`.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---|---|---|---|
| `src/phaze/routers/agent_metadata.py` | 41-50 | `body.model_dump()` without `exclude_unset=True` on a schema where every field is `Optional[...] = None` | 🛑 Blocker | Partial PUT clobbers unset fields. CR-01 in REVIEW.md. Verified end-to-end. |
| `src/phaze/routers/agent_execution.py` | 111-116 | Terminal-state guard runs before any same-status carve-out | 🛑 Blocker | Idempotent retry of COMPLETED PATCH returns 409. CR-02 in REVIEW.md. Verified end-to-end. |
| `src/phaze/routers/agent_metadata.py`, `agent_fingerprint.py`, `agent_execution.py` | (various) | No verification that `file_id`/`proposal_id` belongs to the authenticated agent | ⚠️ Warning | WR-01 in REVIEW.md — explicitly accepted as T-25-04-T deferred to Phase 29. Cross-agent writes currently permitted. Visible audit recommended. |
| `src/phaze/routers/agent_files.py` | 60-65 | NFC normalizes `original_path` but not `current_path` or `original_filename` | ⚠️ Warning | WR-02 in REVIEW.md. Mixed Unicode normalization within one row will cause silent downstream comparison failures. |
| `src/phaze/routers/agent_files.py` | 105 | `ext = "." + row.file_type.lower()` — no defense against agent sending `".mp3"` (double-dot becomes `"..mp3"` → silent skip) | ⚠️ Warning | WR-03. Schema accepts any 1-10 char string. Silent failure mode — `enqueued` count silently lower than expected. |
| `tests/conftest.py` | 39-45 | Test DB uses `Base.metadata.create_all` — migration 014's partial index never created in router test DB | ⚠️ Warning | WR-04. Partial-index predicate drift invisible to CI. Production index never validated under test query plan. |
| `src/phaze/schemas/agent_heartbeat.py` | 14-17 | `worker_pid: int`, `queue_depth: int` — no bounds | ℹ️ Info | WR-07. Low risk on private LAN; defensive validation recommended. |
| `src/phaze/routers/agent_files.py` | 108-114 | `except Exception: logger.exception(...)` swallows enqueue failures without a counter | ℹ️ Info | WR-08. Debuggability hazard until Phase 27 ships re-enqueue UI. |

### Human Verification Required

#### 1. End-to-end agent retry walk-through

**Test:** After fixing the two BLOCKER idempotency bugs, drive a real httpx client through the canonical retry scenarios:
- Network glitch after COMPLETED PATCH → SAQ retries → re-send same PATCH → expect 200.
- Partial metadata PUT after a full PUT → confirm prior fields preserved.
- Same chunk of file upserts replayed → confirm no duplicates (this case already works).

**Expected:** All three scenarios return success and preserve the final state defined by Success Criterion #3.
**Why human:** Real-world retry behavior under network failure is the operational concern Success Criterion #3 was written to address. The current automated tests have demonstrably missed both bugs because they only exercise the happy paths the implementer thought to write.

#### 2. OpenAPI lock-icon rendering on production /docs

**Test:** Open `/docs` against the running production app and confirm every `/api/internal/agent/*` route shows the lock icon and a 401 challenge.
**Expected:** All 6 routes display the lock; bearer scheme is `components.securitySchemes.bearerAuth`.
**Why human:** UI rendering is visual; the smoke-app `test_openapi_bearer_scheme` covers the underlying contract but not the rendered output.

### Gaps Summary

**Two BLOCKER findings from `25-REVIEW.md` are confirmed in the codebase and reproduced end-to-end against the production routers wired into `create_app()`. Both directly contradict Success Criterion #3 ("Replaying the same ... execution-log PATCH ... produces ... the same final state" / "Replaying the same chunk of file upserts ... produces no duplicate rows and the same final state").**

**Bug CR-02 — ExecutionLog terminal-state idempotent retry returns 409**
- File: `src/phaze/routers/agent_execution.py:107-122`
- Reproduction: POST COMPLETED row, then PATCH `{status: completed}` against same id → server returns `409 {"detail": "execution-log status is terminal"}`. Expected: 200.
- Root cause: terminal guard at line 111 runs before any same-status carve-out. The handler's own docstring (line 99-100) claims "same-status PATCH allowed for idempotent retry" but the implementation doesn't honor this when the row is in COMPLETED or FAILED — which is the most common retry case (agent reports "done" → network drops the response → SAQ retries the job → agent re-sends).
- Test gap: `test_terminal_state_rejects_patch` covers COMPLETED→IN_PROGRESS (a regression). `test_same_status_patch_allowed` covers IN_PROGRESS→IN_PROGRESS (non-terminal same-status). Neither exercises the canonical COMPLETED→COMPLETED retry.
- Fix: `if cur in _TERMINAL and new != cur: raise 409`. Add `test_same_status_patch_terminal_allowed`.

**Bug CR-01 — Metadata partial PUT clobbers unset fields with NULL**
- File: `src/phaze/routers/agent_metadata.py:41-50`
- Reproduction: PUT `{artist:'A', title:'T', year:1992, album:'X'}` against file_id, then PUT `{artist:'B'}` against same file_id → row ends up with `artist='B'` but `title=None, year=None, album=None`.
- Root cause: `body.model_dump()` without `exclude_unset=True`, combined with `Optional[...] = None` schema defaults, means `update_keys` is always the full 9-key set and the SET clause writes NULL for every unset field via `stmt.excluded[k]`.
- Test gap: `test_metadata_replay_overwrites` sends `{artist, title}` on BOTH replays, so it never exercises a partial-replay clobber.
- Fix: `dumped = body.model_dump(exclude_unset=True)` and derive everything from `dumped`. Add a partial-PUT regression test.

**Phase 26 readiness:** Phase 26 will build the agent-side HTTP client. Shipping it against a server that 409s legitimate idempotent retries and corrupts partial metadata writes guarantees Phase 26 will need defensive workarounds (e.g., agent must always send all metadata fields; agent must classify 409-terminal as "swallow if my own write"). Fixing here, on the server, is the correct boundary.

**The auth, surface coverage, revocation, and `agent_id`-from-token contract (SCs 1, 2, 4, 5) are all correctly implemented.** Verification status is `gaps_found` rather than `failed` because the surface scaffolding is good — but the idempotency contract that defines the phase's purpose is broken at two of five paths, and that's not acceptable to ship.

---

_Verified: 2026-05-11_
_Verifier: Claude (gsd-verifier)_
