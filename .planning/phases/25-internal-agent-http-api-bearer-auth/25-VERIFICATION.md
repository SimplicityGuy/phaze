---
phase: 25-internal-agent-http-api-bearer-auth
verified: 2026-05-12T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 3/5
  gaps_closed:
    - "CR-01: agent_metadata.py partial-PUT silently nulls existing columns (closed by Plan 25-07)"
    - "CR-02: PATCH against terminal ExecutionLog with same status returns 409 (closed by Plan 25-08)"
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "OpenAPI lock-icon rendering on production /docs"
    expected: "All 6 /api/internal/agent/* routes display a lock icon in Swagger UI and bearerAuth challenge is wired into the operation security blocks"
    why_human: "Swagger UI rendering is visual; the underlying schema is locked by `test_openapi_bearer_scheme` but the rendered output cannot be asserted programmatically"
---

# Phase 25: Internal Agent HTTP API & Bearer Auth Verification Report (Re-Verification)

**Phase Goal:** The application server exposes an authenticated, idempotent HTTP surface that agents can call to record every state change, with `agent_id` derived from the bearer token and never trusted from request bodies.

**Verified:** 2026-05-12
**Status:** passed
**Re-verification:** Yes — after gap closure (Plans 25-07 and 25-08)

## Re-Verification Summary

| Item | Previous (2026-05-11) | Current (2026-05-12) |
|------|----------------------|----------------------|
| Status | `gaps_found` | `passed` |
| Score | 3/5 | 5/5 |
| CR-01 (Metadata partial-PUT NULL clobber) | ✗ FAILED | ✓ CLOSED |
| CR-02 (ExecutionLog terminal same-status 409) | ✗ FAILED | ✓ CLOSED |
| Phase-25 cohort tests | 33 passed | 38 passed (+5) |
| Regressions introduced | n/a | none |

Both blocker gaps from the initial verification are resolved. The metadata router now uses `body.model_dump(exclude_unset=True)` so partial PUTs preserve unset fields (field-level last-write-wins per D-14). The execution-log router's terminal-state guard now reads `if cur in _TERMINAL and new != cur:` so same-status replays against terminal rows return 200 (canonical idempotent retry), while genuine terminal→other-state attempts still return 409 (D-15 monotonic ladder preserved).

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth (ROADMAP success criterion) | Status | Evidence |
|---|---|---|---|
| 1 | Every `/api/internal/agent/*` route requires a bearer token; unauthenticated → 401, unknown/revoked → 403 | ✓ VERIFIED | `src/phaze/routers/agent_auth.py` exports `get_authenticated_agent` (HTTPBearer with `auto_error=True` → 401, in-function `HTTPException(403, "Forbidden")` for unknown/revoked). All 5 routers `Depends(get_authenticated_agent)`. Tests `test_missing_header_returns_401`, `test_malformed_header_returns_401`, `test_unknown_token_returns_403` all pass. Re-verified — unchanged by gap closure. |
| 2 | `agent_id` resolved by hashing bearer token + lookup; any `agent_id` field in body is ignored or rejected | ✓ VERIFIED | All request schemas declare `model_config = ConfigDict(extra="forbid")`. Every router stamps `agent.id` from `Depends(get_authenticated_agent)` rather than reading from body. `test_metadata_extra_field_422` (still passes after gap closure) asserts 422 with `loc=["body", "agent_id"]` for forged-body attempts. Re-verified — gap closure did not alter auth attribution semantics. |
| 3 | Replaying the same chunk of file upserts, the same proposal mutation, or the same execution-log PATCH with the same natural keys produces no duplicate rows AND the same final state | ✓ VERIFIED | **Both previously-failing idempotency contracts are now closed.** (a) **CR-01:** `agent_metadata.py:52` now uses `body.model_dump(exclude_unset=True)` and derives the SET clause from `dumped` keys only (`set_={k: stmt.excluded[k] for k in dumped}` on line 63). Empty-body PUT falls back to `on_conflict_do_nothing(index_elements=["file_id"])` (line 68). Regression test `test_metadata_partial_put_preserves_other_fields` asserts that PUT `{artist:'Aphex Twin', title:'Xtal', year:1992, album:'SAW85-92'}` followed by PUT `{artist:'Aphex Twin v2'}` preserves `title`, `year`, `album` — passes. (b) **CR-02:** `agent_execution.py:117` now reads `if cur in _TERMINAL and new != cur:` — same-status replays against terminal rows fall through both guards and return 200. Regression tests `test_same_status_patch_terminal_allowed` (COMPLETED→COMPLETED → 200), `test_same_status_patch_terminal_failed_allowed` (FAILED→FAILED → 200), and `test_terminal_completed_to_failed_still_rejected` (COMPLETED→FAILED → 409) all pass. File-upsert idempotency (`test_replay_no_duplicates`) and ExecutionLog POST replay (`test_create_replay_no_op`) continue to work. |
| 4 | Setting `agents.revoked_at` immediately causes next call to be rejected with no app-server restart required | ✓ VERIFIED | `agent_auth.py` uses `Agent.revoked_at.is_(None)` predicate (matches partial index `ix_agents_token_hash_active WHERE revoked_at IS NULL` from migration 014). Module docstring forbids caching. `test_revoke_blocks_next_call` and `test_heartbeat_revoke_blocks_next_call` are the active regression guards — both pass. Re-verified — unchanged by gap closure. |
| 5 | API surface covers file upsert, metadata write, fingerprint write, execution-log create/patch, heartbeat — all callable end-to-end | ✓ VERIFIED | Production app verification: `python -c "from phaze.main import create_app; app = create_app(); [print(r.path) for r in app.routes if 'agent' in str(r.path)]"` enumerates all 6 paths: `/api/internal/agent/files`, `.../metadata/{file_id}`, `.../fingerprints/{file_id}/{engine}`, `.../execution-log`, `.../execution-log/{execution_log_id}`, `.../heartbeat`. `main.py` lines 64-68 wire all 5 routers. Re-verified — unchanged by gap closure. |

**Score:** 5/5 truths verified. All Success Criteria from ROADMAP are now observably true.

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `src/phaze/routers/agent_auth.py` | Bearer auth helper | ✓ VERIFIED | Unchanged by gap closure. Imported by all 5 routers. |
| `src/phaze/routers/agent_files.py` | POST /api/internal/agent/files | ✓ VERIFIED | Unchanged by gap closure. Composite UQ UPSERT, xmax INSERT detection, NFC normalization, per-agent SAQ enqueue. |
| `src/phaze/routers/agent_metadata.py` | PUT /api/internal/agent/metadata/{file_id} | ✓ VERIFIED | **PROMOTED from ⚠️ HOLLOW → ✓ VERIFIED.** Plan 25-07 replaced the broken `body.model_dump()` call with `body.model_dump(exclude_unset=True)` and added an `on_conflict_do_nothing` fallback for empty-body PUTs. Idempotent-replay contract now correct at the field level. |
| `src/phaze/routers/agent_fingerprint.py` | PUT /api/internal/agent/fingerprints/{file_id}/{engine} | ✓ VERIFIED | Unchanged by gap closure. Composite UQ `(file_id, engine)` UPSERT. |
| `src/phaze/routers/agent_execution.py` | POST + PATCH /api/internal/agent/execution-log | ✓ VERIFIED | **PROMOTED from ⚠️ HOLLOW → ✓ VERIFIED.** Plan 25-08 narrowed the terminal-state guard from `if cur in _TERMINAL:` to `if cur in _TERMINAL and new != cur:` — the canonical idempotent retry case (same-status PATCH against terminal) now returns 200. D-15 monotonic ladder intact. |
| `src/phaze/routers/agent_heartbeat.py` | POST /api/internal/agent/heartbeat → 204 | ✓ VERIFIED | Unchanged by gap closure. |
| `alembic/versions/014_add_last_status_to_agents.py` | last_status JSONB + partial token-hash index | ✓ VERIFIED | Unchanged by gap closure. Roundtrip migration verified. |
| `src/phaze/main.py` | All 5 agent routers wired | ✓ VERIFIED | Lines 64-68 verified, 6 paths exposed at runtime. |
| `src/phaze/config.py` | `agent_token_prefix` + `agent_file_chunk_max` settings | ✓ VERIFIED | Unchanged. |
| `tests/conftest.py` | `seed_test_agent` + `authenticated_client` fixtures | ✓ VERIFIED | Unchanged. |
| `tests/test_routers/test_agent_metadata.py` | Regression tests for CR-01 | ✓ VERIFIED | 5 tests total (3 original + 2 new from Plan 25-07): `test_metadata_partial_put_preserves_other_fields`, `test_metadata_empty_put_is_noop_for_existing_row` both pass. |
| `tests/test_routers/test_agent_execution.py` | Regression tests for CR-02 | ✓ VERIFIED | 10 tests total (7 original + 3 new from Plan 25-08): `test_same_status_patch_terminal_allowed`, `test_same_status_patch_terminal_failed_allowed`, `test_terminal_completed_to_failed_still_rejected` all pass. |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| All 5 agent routers | `agent_auth.get_authenticated_agent` | `Depends(get_authenticated_agent)` | ✓ WIRED | All 5 router files import and depend on it. Unchanged. |
| `agent_auth.py` SELECT | migration 014 partial index | `Agent.revoked_at.is_(None)` | ✓ WIRED | SQLAlchemy `.is_(None)` renders `WHERE revoked_at IS NULL`, matches partial-index predicate. Unchanged. |
| `agent_files.py` enqueue | per-agent SAQ queue | `Queue.from_url(redis_url, name=f"phaze-agent-{agent.id}")` | ✓ WIRED | Unchanged. |
| `main.py` include_router | each phase-25 router | `app.include_router(...)` | ✓ WIRED | 5 calls present, 6 production paths enumerated. Unchanged. |
| `agent_metadata.py` PUT | `FileMetadata` natural-key UPSERT | `pg_insert(...).on_conflict_do_update(index_elements=["file_id"], set_={k: stmt.excluded[k] for k in dumped})` | ✓ WIRED-AND-CORRECT | **Was WIRED-BUT-INCORRECT.** Plan 25-07 fixed the SET clause to derive from `dumped` (only explicitly-set keys), not the static full schema field set. Field-level idempotency restored. |
| `agent_metadata.py` PUT (empty-body) | `FileMetadata` UPSERT | `on_conflict_do_nothing(index_elements=["file_id"])` | ✓ WIRED | New branch added by Plan 25-07 to avoid Postgres empty-SET-clause syntax error when `dumped == {}`. |
| `agent_execution.py` PATCH | monotonic guard | `_STATUS_ORDER` ladder + `_TERMINAL` frozenset + `cur in _TERMINAL and new != cur` carve-out | ✓ WIRED-AND-CORRECT | **Was WIRED-BUT-INCORRECT.** Plan 25-08 added `and new != cur` operator to the terminal-state guard. Lifecycle ladder intact; backward transitions still 409; same-status terminal retries now 200. |

### Data-Flow Trace (Level 4)

This phase produces HTTP endpoints, not rendered components. Data flow is request-payload → handler → DB. Validated end-to-end via the regression test suite, which uses real Postgres (test DB) and the production router imports (`from phaze.routers.agent_metadata import router as agent_metadata_router`, etc.). All 38 phase-25 cohort tests pass against the same router code that `create_app()` registers in production (`grep "include_router(agent_" src/phaze/main.py` returns the same 5 modules).

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Production app exposes 6 agent routes | `python -c "from phaze.main import create_app; ..."` enumeration | All 6 `/api/internal/agent/*` paths registered | ✓ PASS |
| All phase-25 cohort tests pass | `uv run pytest tests/test_routers/test_agent_*.py tests/test_services/test_agent_upsert.py` | `38 passed in 7.42s` (was 33; +5 from gap-closure regression tests) | ✓ PASS |
| Metadata tests pass (incl. CR-01 regression) | `uv run pytest tests/test_routers/test_agent_metadata.py -v` | `5 passed` | ✓ PASS |
| Execution tests pass (incl. CR-02 regression) | `uv run pytest tests/test_routers/test_agent_execution.py -v` | `10 passed` | ✓ PASS |
| 401 + WWW-Authenticate on missing header | `test_missing_header_returns_401` | passes | ✓ PASS |
| 403 on unknown bearer | `test_unknown_token_returns_403` | passes | ✓ PASS |
| Revoke mid-session blocks next call | `test_revoke_blocks_next_call` | passes | ✓ PASS |
| File-upsert replay produces 1 row | `test_replay_no_duplicates` | passes | ✓ PASS |
| ExecutionLog POST replay = no-op | `test_create_replay_no_op` | passes | ✓ PASS |
| **CR-01: Metadata partial-PUT preserves prior fields** | `test_metadata_partial_put_preserves_other_fields` — PUT full payload, then PUT `{artist}`, assert title/year/album survive | passes — `title="Xtal"`, `year=1992`, `album="SAW85-92"` all preserved | ✓ PASS |
| **CR-01: Empty-body PUT is no-op for existing row** | `test_metadata_empty_put_is_noop_for_existing_row` — PUT `{artist, title}`, then PUT `{}`, assert row unchanged | passes, returns 200, row preserved | ✓ PASS |
| **CR-02: COMPLETED→COMPLETED PATCH returns 200** | `test_same_status_patch_terminal_allowed` — POST COMPLETED, PATCH `{status: completed}` | passes, returns 200, row stays COMPLETED | ✓ PASS |
| **CR-02: FAILED→FAILED PATCH returns 200** | `test_same_status_patch_terminal_failed_allowed` — POST FAILED, PATCH `{status: failed}` | passes, returns 200 | ✓ PASS |
| **CR-02: Terminal→other-terminal still 409** | `test_terminal_completed_to_failed_still_rejected` — POST COMPLETED, PATCH `{status: failed}` | passes, returns 409 `"execution-log status is terminal"` | ✓ PASS |
| Ruff (all 4 modified files) | `uv run ruff check src/phaze/routers/agent_metadata.py src/phaze/routers/agent_execution.py tests/test_routers/test_agent_metadata.py tests/test_routers/test_agent_execution.py` | `All checks passed!` | ✓ PASS |
| Mypy (both routers) | `uv run mypy src/phaze/routers/agent_metadata.py src/phaze/routers/agent_execution.py` | `Success: no issues found in 2 source files` | ✓ PASS |
| CR-01 router grep gates | `grep -c "exclude_unset=True"` = 2 (1 code + 1 docstring), `grep -c "body.model_dump()"` = 0, `grep -c "on_conflict_do_nothing"` = 1, `grep -c "for k in dumped"` = 1, `grep -c "CR-01"` = 2 | all pass plan acceptance criteria | ✓ PASS |
| CR-02 router grep gates | `grep -c "cur in _TERMINAL and new != cur"` = 1, `grep -cE "if cur in _TERMINAL:[[:space:]]*$"` = 0 (old buggy guard gone), `grep -c "CR-02"` = 2, error message strings unchanged | all pass plan acceptance criteria | ✓ PASS |

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
|---|---|---|---|---|
| AUTH-01 | 25-01, 25-02, 25-03, 25-04, 25-05, 25-07, 25-08 | Bearer-token auth; `agent_id` from token, never from body | ✓ SATISFIED | `agent_auth.get_authenticated_agent` correct; `extra="forbid"` on all request schemas; routers stamp `agent.id` from auth dep. Gap closures did not alter this. |
| AUTH-04 | 25-01, 25-02, 25-04 | Revoke immediately blocks; no restart required | ✓ SATISFIED | `.is_(None)` predicate + partial index + no in-process cache. `test_revoke_blocks_next_call` is the active regression guard. Unchanged by gap closure. |
| DIST-04 | 25-01, 25-03, 25-04, 25-05 | Authenticated HTTPS calls cover every state change (file upsert, metadata, fingerprint, exec-log, heartbeat) | ✓ SATISFIED | All 5 surfaces exist, auth-gated, wired into production app. 6 paths enumerated at runtime. Unchanged by gap closure. |
| DIST-05 | 25-01, 25-03, 25-04, 25-05, 25-07, 25-08 | Every endpoint is idempotent on retry; natural keys guarantee replay safety | ✓ SATISFIED | **PROMOTED from ✗ BLOCKED → ✓ SATISFIED.** Both broken idempotency contracts are now closed by Plans 25-07 (CR-01: metadata field-level LWW) and 25-08 (CR-02: terminal same-status PATCH carve-out). All idempotency assertions in the cohort tests pass: file-upsert replay = 1 row; metadata partial-PUT preserves prior fields; metadata empty-PUT no-op; ExecutionLog POST replay = no-op; ExecutionLog same-status PATCH (terminal or non-terminal) = 200; ExecutionLog terminal→other still = 409. |

No orphaned requirements: REQUIREMENTS.md maps DIST-04, DIST-05, AUTH-01, AUTH-04 all to Phase 25, and all four appear in at least one plan's `requirements` field (gap closures 25-07/25-08 also list DIST-05, DIST-04, AUTH-01).

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---|---|---|---|
| `src/phaze/routers/agent_metadata.py` | 55-68 | Empty-body PUT against a brand-new (no prior row) `file_id` still INSERTs a "ghost" row with only `{file_id, id}` (every other column NULL) | ⚠️ Warning | Per gap-closure review WR-01: untested code path. If a client misroutes empty `{}` to a never-seen `file_id`, a metadata row is materialized with no real metadata. Downstream code that uses "metadata row exists" as a proxy for "we extracted metadata" will be misled. Out of scope for CR-01 itself (which targets the partial-PUT case); flag for follow-up. |
| `tests/test_routers/test_agent_execution.py` | 319-348 | `test_same_status_patch_terminal_failed_allowed` lacks the DB-side row-state assertion its COMPLETED twin has | ⚠️ Warning | Per gap-closure review WR-02: asymmetric coverage. A future bug that quietly mutates row status on same-status FAILED PATCH would slip past this test. Trivial fix — add `assert row.status == ExecutionStatus.FAILED` after the response check. Doesn't block this phase but worth tightening before Phase 28 (which exercises this code path harder). |
| `src/phaze/routers/agent_metadata.py` | 51-64 | Explicit `null` in PUT body is silently treated as "clear this field" — undocumented and untested | ℹ️ Info | Per gap-closure review IN-02: this is correct PATCH/partial-PUT semantics but ambiguous in the docstring. No regression test exercises `{"artist": null}`. Document or add a quick test. |
| `tests/test_routers/test_agent_execution.py` | 350-381 | `test_terminal_completed_to_failed_still_rejected` does not assert the seed POST succeeded | ℹ️ Info | Per gap-closure review IN-01: if seed POST silently fails, PATCH 404s instead of 409, misleading triage. Add `assert r_post.status_code == 200`. |
| `src/phaze/routers/agent_metadata.py` | 46-49 | New docstring narrates the historical bug ("Previously the dump call was invoked without `exclude_unset=True`...") | ℹ️ Info | Per gap-closure review IN-03: useful sprint-traceability but will be dead text once verification is closed. Bookkeeping; trim in a future pass. |
| `src/phaze/routers/agent_metadata.py` | 55 | `payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}` generates a UUID4 on every request even when it will be discarded | ℹ️ Info | Per gap-closure review IN-04: harmless; not worth refactoring. |
| `src/phaze/routers/agent_metadata.py`, `agent_fingerprint.py`, `agent_execution.py` | (various) | No verification that `file_id`/`proposal_id` belongs to the authenticated agent | ⚠️ Warning | Carried forward from initial verification — WR-01 in `25-REVIEW.md`. Explicitly accepted as T-25-04-T deferred to Phase 29. Cross-agent writes still permitted. |
| `src/phaze/routers/agent_files.py` | 60-65 | NFC normalizes `original_path` but not `current_path` or `original_filename` | ⚠️ Warning | Carried forward — WR-02 in `25-REVIEW.md`. |
| `src/phaze/routers/agent_files.py` | 105 | `ext = "." + row.file_type.lower()` — no defense against agent sending `".mp3"` (double-dot) | ⚠️ Warning | Carried forward — WR-03 in `25-REVIEW.md`. |
| `tests/conftest.py` | 39-45 | Test DB uses `Base.metadata.create_all` — migration 014's partial index never created in router test DB | ⚠️ Warning | Carried forward — WR-04 in `25-REVIEW.md`. |
| `src/phaze/routers/agent_files.py` | 108-114 | `except Exception: logger.exception(...)` swallows enqueue failures without a counter | ℹ️ Info | Carried forward — WR-08 in `25-REVIEW.md`. Mitigated when Phase 27 ships re-enqueue UI. |

**No new blockers.** All warnings are either follow-up tightenings explicitly flagged by the gap-closure code review (and not part of the gap-closure scope) or were already accepted by the initial verification.

### Human Verification Required

#### 1. OpenAPI lock-icon rendering on production `/docs`

**Test:** Open `/docs` against the running production app (`uv run uvicorn phaze.main:app --reload` or via the docker-compose `application-server`) and confirm every `/api/internal/agent/*` route shows the lock icon and a 401 challenge.
**Expected:** All 6 routes display the lock; bearer scheme is `components.securitySchemes.bearerAuth`.
**Why human:** Swagger UI rendering is visual. The underlying contract is locked by `test_openapi_bearer_scheme` (asserts `bearerAuth` scheme with `type=http, scheme=bearer`) but the rendered output cannot be programmatically asserted.

> The previous verification listed a second human-verification item (end-to-end agent retry walk-through). That item was contingent on fixing CR-01 and CR-02; both fixes are now locked by regression tests (`test_metadata_partial_put_preserves_other_fields`, `test_same_status_patch_terminal_allowed`, `test_same_status_patch_terminal_failed_allowed`) that exercise the canonical retry scenarios. The walk-through is no longer needed for goal verification.

### Gaps Summary

**All gaps from the initial verification are closed.**

- **CR-01 closed:** `src/phaze/routers/agent_metadata.py:52` uses `body.model_dump(exclude_unset=True)`; `set_={k: stmt.excluded[k] for k in dumped}` on line 63 ensures only client-supplied fields participate in the UPDATE; line 68 falls back to `on_conflict_do_nothing` for empty bodies. Regression locked by `test_metadata_partial_put_preserves_other_fields` (canonical Aphex Twin v2 partial-PUT case) and `test_metadata_empty_put_is_noop_for_existing_row`.

- **CR-02 closed:** `src/phaze/routers/agent_execution.py:117` reads `if cur in _TERMINAL and new != cur:`. Same-status terminal replays fall through to a no-op apply (`exclude_unset=True` setattr loop on line 126 also preserves unset fields). Regression locked by three tests — `test_same_status_patch_terminal_allowed` (COMPLETED→COMPLETED → 200), `test_same_status_patch_terminal_failed_allowed` (FAILED→FAILED → 200), `test_terminal_completed_to_failed_still_rejected` (COMPLETED→FAILED still → 409). Pre-existing tests (`test_terminal_state_rejects_patch`, `test_monotonic_regress_returns_409`, `test_same_status_patch_allowed`) still pass.

**No regressions introduced by the gap closures.** The phase-25 cohort grew from 33 to 38 passing tests (+5 new regression tests); zero failures, zero changes to error message strings, zero new dependencies, zero schema/migration changes. The fixes are tightly scoped: one `model_dump(exclude_unset=True)` change + one `on_conflict_do_nothing` fallback in `agent_metadata.py`; one operator-level change (`and new != cur`) in `agent_execution.py`.

**Phase 26 unblocked.** The agent-side HTTP client (Phase 26) can now ship against the correct server contract — partial metadata PUTs preserve unset fields, terminal same-status retries return 200 — without client-side workarounds.

**Phase goal achieved.** The application server exposes an authenticated, idempotent HTTP surface that agents can call to record every state change, with `agent_id` derived from the bearer token and never trusted from request bodies. All 5 ROADMAP Success Criteria are observably true in the production codebase.

---

_Re-verified: 2026-05-12_
_Verifier: Claude (gsd-verifier)_
_Re-verification mode: gap closure after Plans 25-07 and 25-08_
