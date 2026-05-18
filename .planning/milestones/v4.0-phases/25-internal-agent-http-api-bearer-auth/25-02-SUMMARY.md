---
phase: 25-internal-agent-http-api-bearer-auth
plan: 02
subsystem: api
tags: [fastapi, httpbearer, bearer-auth, sha256, sqlalchemy, rfc6750, openapi, pytest]

# Dependency graph
requires:
  - phase: 25-internal-agent-http-api-bearer-auth
    plan: 01
    provides: Agent.last_status JSONB column, migration 014 partial index ix_agents_token_hash_active, seed_test_agent + authenticated_client fixtures, hashlib + secrets imports in conftest.py
provides:
  - phaze.routers.agent_auth module exporting bearer_scheme (HTTPBearer scheme_name="bearerAuth"), hash_token(s) helper, and async get_authenticated_agent dependency
  - 401 + WWW-Authenticate: Bearer for missing/malformed Authorization header (RFC 6750)
  - 403 {"detail": "Forbidden"} for unknown OR revoked bearer (indistinguishable per D-06)
  - OpenAPI components.securitySchemes.bearerAuth auto-emitted on any app that depends on get_authenticated_agent
  - AUTH-01 (4/4 client-side rows) + AUTH-04 (2/2 rows) covered by six pytest cases (100% line coverage on agent_auth.py)
affects:
  - 25-03 (files router opens with `agent: Annotated[Agent, Depends(get_authenticated_agent)]`)
  - 25-04 (metadata + fingerprint + heartbeat routers same signature)
  - 25-05 (execution-log router same signature)
  - 25-06 (main.py wires the routers; OpenAPI scheme assertion can be promoted from smoke app to real app)

# Tech tracking
tech-stack:
  added: []  # no new dependencies — stdlib hashlib + existing fastapi.security
  patterns:
    - "FastAPI auth dep that returns the resolved ORM row (not bool) so handlers can use `agent.id` directly"
    - "401-vs-403 split: HTTPBearer(auto_error=True) raises 401 with RFC 6750 header; in-function HTTPException(403, 'Forbidden') for unknown/revoked"
    - "Indexed-equality lookup auth: sha256-hex SELECT against partial index covering revoked_at IS NULL — no in-process cache, immediate revocation"
    - "Smoke FastAPI app test pattern: inline `_make_smoke_app(session)` builds a single-route app so tests are parallel-safe and decoupled from later plans"

key-files:
  created:
    - src/phaze/routers/agent_auth.py
    - tests/test_routers/test_agent_auth.py

key-decisions:
  - "DROPPED `from __future__ import annotations` in agent_auth.py — incompatible with FastAPI dependency-injection runtime introspection of `Annotated[AsyncSession, Depends(...)]`; matches duplicates.py/tags.py pattern. This is a DEVIATION from PATTERNS.md (Rule 1)."
  - "Tests assert response.json() == {'detail': 'Forbidden'} byte-for-byte (D-06): no extra fields, no enumeration oracle distinguishing unknown-token vs revoked-token."
  - "Mid-revoke test uses `sa_func.now()` (aliased to avoid name collision with secrets module) — proves revocation visible to next request without create_app restart."
  - "Smoke app strategy (over deferring to Plan 06): tests stay self-contained and parallel-safe regardless of Plans 03–05 landing order."

patterns-established:
  - "Auth helper module pattern: NOT a router (no `APIRouter()`); just three exports (`bearer_scheme`, `hash_token`, `get_authenticated_agent`). Downstream routers import `get_authenticated_agent` and add it as a `Depends(...)` to every handler signature."
  - "OpenAPI bearer-scheme emission: a single `HTTPBearer(scheme_name=\"bearerAuth\", ...)` instance shared across all routes; the name `bearerAuth` lands at `components.securitySchemes.bearerAuth` (lock icon in /docs auto-attached)."
  - "Predicate alignment with migration 014: code uses `Agent.revoked_at.is_(None)` → SQL `WHERE revoked_at IS NULL`. Renders byte-for-byte the partial-index predicate `ix_agents_token_hash_active ... WHERE revoked_at IS NULL`. Postgres picks the partial index for the auth lookup."

requirements-completed:
  - AUTH-01
  - AUTH-04

# Metrics
duration: 6min
completed: 2026-05-12
---

# Phase 25 Plan 02: Agent Bearer-Auth Dependency Summary

**`phaze.routers.agent_auth` helper module — single async FastAPI dependency `get_authenticated_agent` plus `hash_token` and `bearer_scheme`, with six pytest cases proving AUTH-01 (401/403 split + RFC 6750 challenge) and AUTH-04 (immediate revocation + new-token rotation) green at 100% coverage.**

## Performance

- **Duration:** ~6 min
- **Started:** 2026-05-12T00:03:28Z
- **Completed:** 2026-05-12T00:09:23Z
- **Tasks:** 2 of 2 completed
- **Files created:** 2 (1 source, 1 test)

## Accomplishments

- **Auth helper module** `src/phaze/routers/agent_auth.py` (NOT a router per D-09) ships three exports:
  - `bearer_scheme = HTTPBearer(scheme_name="bearerAuth", description="…")` — auto-emits the OpenAPI lock-icon scheme.
  - `hash_token(token: str) -> str` — SHA-256 hex of the FULL wire string (prefix included per D-02).
  - `async def get_authenticated_agent(credentials, session) -> Agent` — single indexed SELECT against the partial index, returns the row or raises 403.
- **Six pytest cases** (`tests/test_routers/test_agent_auth.py`) cover every AUTH-01 + AUTH-04 + OpenAPI VALIDATION.md row:
  - `test_missing_header_returns_401` (AUTH-01 1/4) — 401 + `WWW-Authenticate: Bearer` header
  - `test_malformed_header_returns_401` (AUTH-01 2/4) — non-Bearer scheme → 401
  - `test_unknown_token_returns_403` (AUTH-01 3/4) — well-formed unknown bearer → 403, body `{"detail": "Forbidden"}`
  - `test_revoke_blocks_next_call` (AUTH-04 1/2) — `update(Agent).values(revoked_at=NOW())` between two requests with same bearer → second returns 403 without restart
  - `test_new_token_authenticates` (AUTH-04 2/2) — second agent + new token authenticates cleanly (returns `agent_id == "test-agent-02"`)
  - `test_openapi_bearer_scheme` — `components.securitySchemes.bearerAuth` is `{"type": "http", "scheme": "bearer"}`
- **100% line coverage** on `agent_auth.py` (19 stmts, 0 missing).
- **Full project mypy clean** (85 source files); **full router test suite green** (199 tests).
- **Threat mitigations:** information-disclosure oracle (T-25-02-I) blocked by indistinguishable 403 envelope; cache-breaking tampering (T-25-02-T) blocked by `test_revoke_blocks_next_call` regression guard; predicate-drift tampering blocked by `Agent.revoked_at.is_(None)` byte-for-byte matching migration 014.

## Task Commits

Each task was committed atomically:

1. **Task 1: Failing AUTH-01 + AUTH-04 + OpenAPI tests (RED)** — `d44166b` (test)
2. **Task 2: Implement agent bearer-auth FastAPI dependency (GREEN + Rule 1 fix)** — `251d3d1` (feat)

## Files Created/Modified

- `src/phaze/routers/agent_auth.py` (CREATED, 84 LOC) — Helper module with `bearer_scheme`, `hash_token`, and `get_authenticated_agent`. No `APIRouter()`. Imports `AsyncSession` at runtime (NOT under `TYPE_CHECKING`) so FastAPI dependency-injection can resolve `Annotated[AsyncSession, Depends(get_session)]` at app-build time. SELECT: `select(Agent).where(Agent.token_hash == token_hash, Agent.revoked_at.is_(None))`. 403 path: `raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")`.
- `tests/test_routers/test_agent_auth.py` (CREATED, 143 LOC) — Six pytest-asyncio cases using a self-contained `_make_smoke_app(session)` builder so tests don't depend on Plans 03-05 routers landing first. Reuses `session`, `seed_test_agent` fixtures from Plan 01. `sa_func.now()` aliased to avoid `secrets`/`func` name collision in test 5.

## Decisions Made

- **Drop `from __future__ import annotations` in `agent_auth.py`** (deviation from PATTERNS.md): with future-annotations active, every annotation becomes a string, and Pydantic/FastAPI can't resolve `Annotated[AsyncSession, Depends(get_session)]` from the deferred forward-ref. The OpenAPI generator raises `PydanticUserError: TypeAdapter[...] is not fully defined`. Matching the working pattern of `duplicates.py`/`tags.py`: import `AsyncSession` at runtime and omit `from __future__`. Ruff `TC002` (would normally push the import under `TYPE_CHECKING`) does NOT fire on this file because the annotation is evaluated eagerly and the symbol is genuinely runtime-needed.
- **Smoke-app strategy over deferring OpenAPI test to Plan 06:** rather than wait for `main.py` to wire a real auth-gated route in Wave 4, the test file builds an inline FastAPI app per-test with a `/smoke` route guarded by `Depends(get_authenticated_agent)`. Result: parallel-safe, fully self-contained, no coupling to landing order of Plans 03/04/05.
- **`{"detail": "Forbidden"}` body for both unknown-hash AND revoked-agent** (D-06): test 3 asserts the exact dict so no contributor can later add an `agent_id_known: bool` field that would create an enumeration oracle.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Dropped `from __future__ import annotations` from `agent_auth.py`**

- **Found during:** Task 2 (running `uv run pytest tests/test_routers/test_agent_auth.py -v` after writing the module verbatim from PATTERNS.md)
- **Issue:** With `from __future__ import annotations` + `if TYPE_CHECKING: from sqlalchemy.ext.asyncio import AsyncSession`, FastAPI's signature inspector receives a string forward-ref for the `session` parameter. Pydantic raises `PydanticUserError: TypeAdapter[typing.Annotated[ForwardRef('Annotated[AsyncSession, Depends(get_session)]'), ...]] is not fully defined` when building the OpenAPI schema, breaking 4 of 6 tests (`test_unknown_token_returns_403`, `test_revoke_blocks_next_call`, `test_new_token_authenticates`, `test_openapi_bearer_scheme`).
- **Fix:** Removed `from __future__ import annotations` and moved `AsyncSession` import out of the `TYPE_CHECKING` block. Pattern matches `src/phaze/routers/duplicates.py` and `src/phaze/routers/tags.py` (both ruff-clean, both use runtime `AsyncSession` imports).
- **Files modified:** `src/phaze/routers/agent_auth.py`
- **Verification:** All 6 tests pass (`6 passed in 1.23s`). `uv run ruff check src/phaze/routers/agent_auth.py` clean (TC002 doesn't fire because annotations are evaluated eagerly).
- **Committed in:** `251d3d1` (Task 2 commit)
- **Rationale:** Bug directly blocked Task 2's done criterion ("all six tests pass"); Rule 1 (auto-fix bugs) applies. PATTERNS.md template was authored before this FastAPI-on-asyncpg combination was exercised; updating the pattern guide is a separate documentation task outside this plan's scope.

---

**Total deviations:** 1 auto-fixed (1 Rule 1 bug)
**Impact on plan:** Single targeted import change; no scope creep. The fix is the canonical FastAPI pattern for runtime dependency types and aligns with two existing routers (`duplicates.py`, `tags.py`). Downstream Plans 03/04/05 should follow the same convention (do NOT use `from __future__ import annotations` in router modules that consume `Depends(get_session)`).

## Issues Encountered

- **Test file is in a routers test directory** but the *production module* is a helper, not a router. Resolved by leaving the file path as PATTERNS.md specified (`src/phaze/routers/agent_auth.py`) — colocating with other auth-adjacent FastAPI machinery is correct even though the module exports no `APIRouter()`. The done criterion `grep -c APIRouter ... returns 0` is the regression guard.
- **Ruff TC002 (move `AsyncSession` to `TYPE_CHECKING`) was a temporary distraction** until the future-annotations bug was diagnosed; once `from __future__` was removed, TC002 no longer fires (the symbol is genuinely runtime-needed).

## Threat Mitigations Verified

- **T-25-02-S (brute-force spoofing):** Accepted — 2^256 secret entropy from `secrets.token_urlsafe(32)`. Phase 29 will add HTTPS + LAN binding so the wire token never traverses untrusted segments.
- **T-25-02-I (401-vs-403 enumeration oracle):** Mitigated — `test_unknown_token_returns_403` asserts `response.json() == {"detail": "Forbidden"}` byte-for-byte. No `agent_id_known` field, no detail variation between unknown-hash and revoked-agent paths.
- **T-25-02-I (token-in-logs disclosure):** Mitigated — module contains zero `logger.*` calls; no `print(credentials.credentials)`; the only caller-side log signal is `agent.id` after a successful resolution.
- **T-25-02-T (in-process cache defeats revocation):** Mitigated — module-level docstring explicitly forbids caching; `test_revoke_blocks_next_call` is the active regression guard.
- **T-25-02-T (`== None` vs `.is_(None)` predicate drift):** Mitigated — code uses `.is_(None)`; E711 ruff rule blocks `== None`; `test_revoke_blocks_next_call` would fail if the predicate didn't match migration 014's partial index.
- **T-25-02-D (DoS via flood):** Accepted — private LAN deployment, microsecond-scale partial-index probe, connection-pool sized in `database.py`. Revisit if multi-tenant arrives (OPS-06).

## Notes for Downstream Plans (03, 04, 05, 06)

**Exact import statement to use (byte-for-byte):**
```python
from phaze.routers.agent_auth import get_authenticated_agent
```

**Exact handler-signature line every agent-internal route uses (byte-for-byte):**
```python
agent: Annotated[Agent, Depends(get_authenticated_agent)]
```

**Critical conventions for Plans 03/04/05 router files:**
- Do **NOT** put `from __future__ import annotations` at the top of router modules that consume `Depends(get_session)` — FastAPI cannot resolve the forward-ref. Use runtime imports for SQLAlchemy types (matches `duplicates.py`, `tags.py`, and now `agent_auth.py`).
- Import `Agent` from `phaze.models.agent`.
- Import `AsyncSession` from `sqlalchemy.ext.asyncio` (runtime, NOT under `TYPE_CHECKING`).
- Every handler signature begins with `agent: Annotated[Agent, Depends(get_authenticated_agent)]` BEFORE the `session: AsyncSession = Depends(get_session)` parameter — this makes the auth gate visually unmistakable in code review.

**For Plan 06 (main.py wiring):**
- The OpenAPI `bearerAuth` scheme automatically appears on `/openapi.json` as soon as the first router using `get_authenticated_agent` is included via `app.include_router(...)`. No explicit `Security(...)` decoration needed.
- After Plan 06 wires the real routers into `main.py`, Plan 25-02's smoke-app `test_openapi_bearer_scheme` can be **complemented** (not replaced) by an integration test that asserts `bearerAuth` appears on the real `create_app()` openapi.json.

**For test authors:**
- Use the `authenticated_client` fixture (Plan 01) when you want a happy-path call. The Authorization header is pre-set.
- Use `seed_test_agent` directly when you need the raw token (e.g., to revoke mid-test, or to test 401/403 with a forged variation).
- The `session` fixture commits before the `AsyncClient` opens, so the `Depends(get_session)`-yielded session inside the handler sees the seeded agent.

## Self-Check: PASSED

**Files verified to exist:**
- `src/phaze/routers/agent_auth.py` (CREATED)
- `tests/test_routers/test_agent_auth.py` (CREATED)

**Commits verified in git log:**
- `d44166b` — test(25-02): add failing AUTH-01 + AUTH-04 + OpenAPI tests for agent auth dep
- `251d3d1` — feat(25-02): implement agent bearer-auth FastAPI dependency

**Coverage verified:**
- `agent_auth.py`: 19 stmts, 0 missing, 100.00% coverage (≥95% gate met).

**Final gates verified:**
- `uv run pytest tests/test_routers/test_agent_auth.py -v` → `6 passed`
- `uv run mypy src/phaze/routers/agent_auth.py` → `Success: no issues found in 1 source file`
- `uv run mypy .` → `Success: no issues found in 85 source files` (full project clean)
- `uv run ruff check src/phaze/routers/agent_auth.py tests/test_routers/test_agent_auth.py` → `All checks passed!`
- `uv run pytest tests/test_routers/ -q` → `199 passed` (no regression in sibling router tests)
- `pre-commit run --files src/phaze/routers/agent_auth.py tests/test_routers/test_agent_auth.py` → all hooks Passed
- `grep -c "APIRouter" src/phaze/routers/agent_auth.py` → `0` (D-09: NOT a router)
- `bearer_scheme.scheme_name` at runtime → `"bearerAuth"` (OpenAPI lock-icon scheme name)
- `hash_token('phaze_agent_test') == hashlib.sha256(b'phaze_agent_test').hexdigest()` → True (hash invariant)

---
*Phase: 25-internal-agent-http-api-bearer-auth*
*Plan: 02*
*Completed: 2026-05-12*
