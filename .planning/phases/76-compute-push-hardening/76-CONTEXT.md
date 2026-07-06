# Phase 76: Compute/Push Hardening - Context

**Gathered:** 2026-07-06
**Status:** Ready for planning
**Source:** Operator brief (2026-07-06 disposition session) + code-verified anchors + one clarifying decision (HARD-03 target)

<domain>
## Phase Boundary

The milestone's LAST phase (2026.7.2 Multi-Compute Agents). Exactly **three self-contained
correctness fixes** in the N-compute dispatch/push path, each closing an accepted-risk or
code-review item surfaced during Phases 72-74, each shipping its own regression test. Category
HARD. **No new dependencies** (`pyproject.toml` / `uv.lock` untouched). Coverage stays ≥ the
enforced gate; `just docs-drift` stays green. Ships as its own PR on worktree branch
`SimplicityGuy/phase-76` (never direct to main).

**In scope:** HARD-01 (probe session-safety), HARD-02 (ledger RMW atomicity), HARD-03 (agent_id
boundary validation) — and only these.

**Out of scope:** any new routing semantics, provisioning, feature work, or scope expansion. The
older posture-based accepted risks (AR-27-* CSRF, AR-37-* app-layer auth, AR-51-08 SAQ
schema-CREATE) stay ACCEPTED (deployment posture unchanged: single-user, private-LAN,
reverse-proxy internal auth). AR-73-01 (N-compute per-agent orphan recovery) is folded into v2
**PROV-01**, NOT this phase (it is a feature with Phase-45-class over-enqueue risk).
</domain>

<decisions>
## Implementation Decisions

### HARD-01 — N-compute liveness probe session-safety (closes WR-01 / 74-REVIEW)

- **D-01:** Fix `services/backends.py:665` `_probe_availability` by **serializing** the probe
  fan-out — replace `results = await asyncio.gather(*(_probe_one(session, backend) for backend in backends))`
  with a sequential loop that `await`s each `_probe_one(session, backend)` one at a time and
  builds the `{backend_id: available}` dict. Chosen over "give each `_probe_one` its own session
  from the sessionmaker" because N is tiny (a handful of backends on a 5s poll), serial keeps the
  single request-scoped `session` (no sessionmaker plumbing into the read path), and it removes
  the concurrent-shared-`AsyncSession` hazard entirely (N≥2 compute lanes each call
  `session.execute` via `select_agent_by_id`, which is SQLAlchemy-unsafe under `gather`).
- **D-02:** Keep the bounded `asyncio.wait_for(backend.is_available(session), _PROBE_TIMEOUT_SEC)`
  **inside** `_probe_one` (backends.py:644) unchanged, and keep the post-fan-out
  `await session.rollback()` in `get_backend_lane_snapshot` (backends.py:702). Serializing changes
  concurrency, not the per-probe timeout budget or the poison-clearing rollback.
- **D-03:** Reword the `_probe_availability` docstring (backends.py:651-663) and the inline
  "Session-safety (Pitfall 1) … proven race-free in practice by the Plan 74-03 Variant B arbiter
  test" comment from an **empirical** claim to a **structural guarantee** ("probes run
  sequentially on the one session, so there is never concurrent use"). Also update the `_probe_one`
  reference to the fan-out being concurrent (backends.py:636-639 wording).
- **D-04:** HARD-01 regression test asserts that with **N≥2 online compute backends**, the probe
  yields correct, **deterministic** per-backend `available` — a structural test (sequential
  execution), NOT the empirical repeated-runs arbiter approach Plan 74-03 used. If the Plan 74-03
  Variant-B arbiter test exists, supersede/retarget it toward the deterministic assertion.

### HARD-02 — push_attempt ledger RMW atomicity (closes AR-73-02 / T-73-13 / WR-04)

- **D-05:** Fix `routers/agent_push.py:226` by adding **`.with_for_update()`** to the
  `SchedulingLedger` SELECT that reads `push_attempt` for the read-modify-write:
  `select(SchedulingLedger).where(SchedulingLedger.key == ledger_key).with_for_update()`. This
  makes the read→+1→write-back atomic so concurrent `/mismatch` for one file cannot lose an
  increment. The D-06/D-07 reporter-authorization gate (agent_push.py:214-223) and the CR-01 CAS
  "only spill if still PUSHING" guard (agent_push.py:238-242) stay exactly as-is.
- **D-06:** HARD-02 regression test: two **concurrent** `/mismatch` calls for one file increment
  `push_attempt` **exactly twice** (no lost update), and the bounded `push_max_attempts` cap
  (`config.py`, validator `gt=0 lt=20`) still trips correctly at the boundary. Because row-level
  locking is a real-Postgres behavior, this is a DB-touching test that needs
  `TEST_DATABASE_URL` / `MIGRATIONS_TEST_DATABASE_URL` on port 5433 (`just test-db`).

### HARD-03 — agent_id HTTP-boundary validation (closes AR-30-03 / Phase-30 REVIEW IN-01)

- **D-07:** Harden **BOTH** unvalidated `agent_id` query-param boundaries (operator decision
  2026-07-06 — the brief's line anchor and its "scan-status poll" description pointed at two
  different endpoints; harden both rather than pick one):
  1. `routers/tracklists.py:279` `GET /scan/status` `scan_status` — the literal scan-status poll;
     its `agent_id: str = Query(...)` gains `pattern` + `max_length` args.
  2. `routers/pipeline_scans.py:153` `GET /agent-roots` `agent_roots_swap` — its bare
     `agent_id: str` query param becomes `Annotated[str, Query(pattern=..., max_length=128)]`.
- **D-08:** Use `pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$"` + `max_length=128` — the **established
  agent-id shape**: the `Agent.id` DB CHECK constraint (`models/agent.py:36`) and the CLI
  `AGENT_ID_RE` (`cli/__init__.py:44`). A malformed `agent_id` now returns **422** at the HTTP
  boundary instead of a silently-empty `200` poll (scan_status) / empty picker (agent_roots_swap).
- **D-09:** HARD-03 regression tests: a malformed `agent_id` → **422** for each endpoint
  (`GET /scan/status` and `GET /agent-roots`); a well-formed id still passes through. FastAPI/httpx
  test-client assertions (no DB row-locking needed).

### Cross-cutting

- **D-10:** No new dependencies — do not touch `pyproject.toml` or `uv.lock`. Each fix references
  and closes its accepted-risk/threat in the PLAN threat model: HARD-01 → WR-01 (`74-REVIEW.md`);
  HARD-02 → AR-73-02 / T-73-13 / WR-04; HARD-03 → AR-30-03 / Phase-30 REVIEW IN-01.
- **D-11:** The three fixes are independent (different files, no shared state) — they may plan as
  parallel-wave tasks. `just docs-drift` must stay green; coverage ≥ gate; pre-commit hooks pass
  (never `--no-verify`).

### Claude's Discretion

Plan granularity (one plan-per-fix vs a combined plan), exact test file placement/naming within
the existing `tests/shared/...` tree, and the precise wording of the reworded HARD-01 docstring.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### HARD-01 (probe session-safety)
- `src/phaze/services/backends.py` — `_probe_one` (L632-648), `_probe_availability` (L651-666, the
  `asyncio.gather` at L665 is the fix site), `get_backend_lane_snapshot` (L680+, callers +
  post-fan-out rollback at L702), `_PROBE_TIMEOUT_SEC` (L589).
- `src/phaze/routers/pipeline.py:563` and `:661` — the two `get_backend_lane_snapshot(session)`
  callers (request-scoped session; confirms serial-on-one-session is correct).
- `tests/shared/services/` — existing backends tests (locate the Plan 74-03 Variant-B arbiter
  test to supersede; anchor the new deterministic N≥2 test alongside).

### HARD-02 (ledger RMW atomicity)
- `src/phaze/routers/agent_push.py` — `report_push_mismatch` (L173+), ledger SELECT (L226, fix
  site), reporter-auth gate (L214-223), cap/spill branch (L233+), re-stamp write-back (L333-336).
- `config.py` — `push_max_attempts` setting + its `gt=0 lt=20` validator.
- `src/phaze/models/` — `SchedulingLedger` model (key + payload JSONB) for the test fixture.

### HARD-03 (agent_id boundary validation)
- `src/phaze/routers/tracklists.py:278-291` — `scan_status` (`agent_id: str = Query(...)`, uses
  `task_router.queue_for(agent_id)`).
- `src/phaze/routers/pipeline_scans.py:150-172` — `agent_roots_swap` (`agent_id: str`, uses
  `session.get(Agent, agent_id)`).
- `src/phaze/models/agent.py:36` (`id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'` CHECK) and
  `src/phaze/cli/__init__.py:44` (`AGENT_ID_RE`) — the canonical agent-id pattern to reuse.
- `tests/shared/routers/test_pipeline_scans.py` — existing scans-router tests (anchor the
  agent_roots_swap 422 test); locate/anchor the tracklists scan_status test alongside its router tests.

### Governance
- `.planning/ROADMAP.md` (Phase 76 detail section) and `.planning/REQUIREMENTS.md` (HARD-01..03).
</canonical_refs>

<specifics>
## Specific Ideas

- HARD-01 diff shape: `results = {}` then `for backend in backends: bid, ok = await _probe_one(session, backend); results[bid] = ok` — or a dict comprehension over a sequential async loop.
- HARD-02 diff shape: append `.with_for_update()` to the L226 SELECT statement only.
- HARD-03 diff shape (tracklists): `agent_id: str = Query(..., pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)`.
- HARD-03 diff shape (pipeline_scans): `agent_id: Annotated[str, Query(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=128)]`.
</specifics>

<deferred>
## Deferred Ideas

- v2 **PROV-01**: N-compute per-agent orphan recovery (folds in AR-73-01). Tracked in
  REQUIREMENTS.md v2, no milestone.
- AR-27-* / AR-37-* / AR-51-08: stay ACCEPTED (posture unchanged) — re-affirmed in the accepted-risk log, not Phase 76.
</deferred>

---

*Phase: 76-compute-push-hardening*
*Context gathered: 2026-07-06 (operator brief + code-verified anchors + HARD-03 both-endpoints decision)*
