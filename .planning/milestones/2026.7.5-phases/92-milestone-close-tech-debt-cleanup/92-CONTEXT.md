# Phase 92: Milestone-Close Tech-Debt Cleanup - Context

**Gathered:** 2026-07-13
**Status:** Ready for planning

<domain>
## Phase Boundary

Pay down the three tech-debt items surfaced by the 2026.7.5 milestone audit
(`.planning/2026.7.5-MILESTONE-AUDIT.md`), before running
`/gsd:complete-milestone 2026.7.5`. Candidate requirement IDs `CLEAN-01..03`
(mapped below). Behavior-preserving except the PERF-02 latency win.

**In scope (the three items):**
1. **CLEAN-01** — PERF-02 follow-up: parallelize the independent reads in
   `get_stage_progress` via `asyncio.gather` and re-measure poll latency at 200K scale.
2. **CLEAN-02** — Make the test suite hermetic under per-bucket CI isolation
   (fixes the 83-01 / 83-03 flake class at its shared root).
3. **CLEAN-03** — Cosmetic doc fixes (stale + duplicated comments). Zero runtime change.

**Out of scope (stay deferred — do NOT pull in):**
- **DENORM-01** (denormalized stage-bitmap column) — v2-deferred; revisited *only if*
  the CLEAN-01 re-measurement proves parallelization insufficient.
- **P85 WR-01..04** (`.limit()`-before-Python-filter in review/tags builders) —
  accepted-deferred per operator decision 2026-07-10.
- **P81 WR-01/02** (recovery-cutover follow-up + metadata payload-retention edge).
- **83-06** (backfill-held compute file mis-routes to local) — routing edge, separate owner.
- **Live-corpus deploy rehearsal** (039 real-corpus + drained shadow-compare) — a separate
  operator gate, blocking for prod deploy only, not for this phase.

</domain>

<decisions>
## Implementation Decisions

### Overall appetite (framing)
- **D-01:** **Thorough.** This is the last phase before milestone close, and the operator
  chose to fix root causes properly rather than surgically patch symptoms — accepting a
  broader regression surface for a cleaner close. This framing drives D-02..D-08 below:
  parallelize *all* independent reads (not just the 3 named), fix the shared conftest
  fixture globally (not just the 2 named buckets), and re-run the full 200K measurement.

### CLEAN-01 — PERF-02 parallelization
- **D-02:** **Parallelize ALL independent reads** in `get_stage_progress` via
  `asyncio.gather` — the 3 enrich `_safe_bucket_counts` reads (metadata/fingerprint/analyze)
  **and** the other independent reads (discovery, scan_search, scrape, match, execute).
  Not just the 3 the audit literally named.
- **D-03:** **Each concurrent read gets its own `AsyncSession`** from the sessionmaker.
  Rationale: all reads currently share ONE session (`pipeline.py:565-567`), and a single
  SQLAlchemy `AsyncSession` / asyncpg connection **cannot** run concurrent queries —
  `asyncio.gather` over one session raises "another operation is in progress". True
  concurrency therefore requires N sessions = N pool connections, fired every 5s poll.
  **Planner MUST confirm pool headroom** against the post-PgBouncer-incident caps
  (app pool raised to 75, `max_db_conn` 80 — see `project_pgbouncer_pool_exhaustion`).
  If headroom is tight, a bounded `asyncio.Semaphore` cap is an acceptable fallback, but the
  default target is full fan-out.
- **D-04:** **Preserve the per-read degrade discipline.** Each read stays wrapped in its
  existing `_safe_count` / `_safe_bucket_counts` GroupingError-safe guard; a single failing
  read degrades to its safe default and never aborts the whole poll or poisons a sibling.
- **D-05:** **Re-run the full 200K synthetic-corpus measurement.** Rebuild/reuse the PERF-02
  measurement harness, measure `/pipeline/stats` poll latency **before/after** parallelization,
  and **record both numbers in `92-VERIFICATION.md`**. The measured result drives the
  DENORM-01 revisit: if still over budget → DENORM-01 is a live v2 candidate; if under →
  DENORM-01 stays killed. (SC1's "measurably reducing poll latency at 200K-file scale" is
  the acceptance bar — a lightweight overlap-only proof does NOT satisfy it.)

### CLEAN-02 — Test-suite hermeticity (83-01 / 83-03 flake class)
- **D-06:** **Fix the shared root globally, not the 2 named buckets.** Root cause lives in
  `tests/conftest.py`: the function-scoped `async_engine` does `create_all`/`drop_all` per
  test against one shared `phaze_test` DB, and committed seed rows (`legacy-application-server`,
  `test-agent-01`) survive teardown races and collide (`pk_agents`). The flake class spans
  many buckets (analyze, agents, pipeline, backends…), so the fix is at the fixture, not
  per-bucket.
- **D-07:** **Mechanism = session-scoped engine + per-test transactional rollback.**
  Create the schema **once** (session-scoped), and wrap each test in a transaction / SAVEPOINT
  that is rolled back on teardown, so no committed row ever survives into the next test.
  This is the strongest isolation and makes the suite hermetic by construction.
  - **CRITICAL constraint:** transactional-rollback isolation **intercepts commits made inside
    the test**. The project's `get_session`-never-commits pattern (mutating routers commit
    themselves; `conftest.py:216` overrides `get_session`; tests then read committed rows from
    an *independent* session — see `project_get_session_never_commits`) will **break** under a
    naive outer transaction. The fix MUST use the SQLAlchemy **join-an-external-transaction +
    restart-SAVEPOINT-after-commit** recipe (`after_transaction_end` → re-`begin_nested()`) so
    in-test commits are visible to sibling sessions yet still rolled back at teardown.
- **D-08:** **De-risk via a full-suite green gate.** The phase does NOT merge until the **full
  ~1750-test suite** passes green under **per-bucket CI isolation** (`just test-bucket <bucket>`
  for every bucket, matching the project isolation standard) — not just the 2 named buckets.
  Nothing lands until the whole suite is hermetic. (This is the acceptance gate for CLEAN-02.)

### CLEAN-03 — Cosmetic doc fixes
- **D-09:** Delete the **duplicated comment block** at `src/phaze/services/backends.py:563-566`
  (lines 563-564 and 565-566 are byte-identical "MKUE-01/D-04: thread THIS backend's KubeConfig…")
  — keep one copy.
- **D-10:** Fix the **stale DISCOVERED-stamp comment** at
  `src/phaze/routers/agent_files.py:133`. Zero runtime change; comment-only.

### Claude's Discretion
- Exact `asyncio.gather` structuring, session acquisition helper shape, and whether a
  `Semaphore` cap is needed (gated on D-03 pool-headroom confirmation).
- The precise `conftest.py` fixture wiring for D-07 (engine scope, transaction/connection
  fixtures, savepoint restart hook), subject to the D-07 constraint and D-08 gate.
- Whether the 200K harness is rebuilt fresh or the prior PERF-02 harness is reused.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone audit (source of all three items)
- `.planning/2026.7.5-MILESTONE-AUDIT.md` — the tech-debt roll-up. PERF-02 over-budget /
  DENORM-01 NO-GO + `asyncio.gather` follow-up recommendation (§ phase-82); 83-01/83-03
  non-hermetic flakes (§ phase-83); `backends.py:563-566` duplicated comment (§ phase-83).
- `.planning/ROADMAP.md` § "Phase 92" (goal, 3 success criteria, out-of-scope list).
- `.planning/REQUIREMENTS.md` — PERF-02 (line ~69), DENORM-01 (line ~107, ~171 deferred verdict).

### CLEAN-01 — PERF-02 parallelization
- `src/phaze/services/pipeline.py` — `get_stage_progress` (def ~488), the ~9 reads it issues,
  `_safe_bucket_counts` (~339) and `_safe_count` degrade-safe helpers (the reads to parallelize).

### CLEAN-02 — Test hermeticity
- `.planning/phases/83-cloud-routing-sidecar-cutover/deferred-items.md` §§ 83-01, 83-03 —
  full root-cause writeup + the two candidate fix mechanisms (transactional rollback vs truncate).
- `tests/conftest.py` — the function-scoped `async_engine` fixture (`create_all`/`drop_all`,
  seed commits) and the `get_session` override (~line 216). The central lever for D-06/D-07.
- MEMORY `project_get_session_never_commits` — the commit-then-independent-session-read pattern
  D-07 must preserve. MEMORY `reference_local_fullsuite_colima_flake` /
  `reference_ci_bucket_isolation` — same flake class context + the per-bucket isolation standard.
- MEMORY `reference_migrations_test_db_port` — test-DB port footgun (5432 vs 5433) for anyone
  running the buckets in isolation.

### CLEAN-03 — Cosmetic
- `src/phaze/services/backends.py:563-566` (duplicated block) and
  `src/phaze/routers/agent_files.py:133` (stale comment).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_safe_count` / `_safe_bucket_counts` in `pipeline.py`: the existing GroupingError-safe
  degrade wrappers — reuse verbatim inside each parallelized task (D-04).
- Prior PERF-02 measurement harness (from Phase 82): reuse for the D-05 200K re-measurement
  if still present; otherwise rebuild the synthetic-corpus generator.
- The SQLAlchemy async sessionmaker / `get_session` factory: source of the per-read sessions in D-03.

### Established Patterns
- `get_session` NEVER commits; mutating routers commit themselves and tests read committed
  rows from an independent session — the invariant D-07 must not break.
- Per-bucket CI isolation (`just test-bucket <bucket>`, `tests/buckets.json`) is the project's
  test-isolation standard — the CLEAN-02 acceptance surface (D-08).
- Pool caps are load-bearing after the PgBouncer exhaustion incident (app pool 75 /
  `max_db_conn` 80) — the constraint D-03 must respect.

### Integration Points
- `get_stage_progress` feeds `/pipeline/stats` (the 5s DAG-console poll) and `_build_dag_context`
  — CLEAN-01 must keep the returned dict shape and the derived `done` buckets byte-identical
  (only latency changes).
- `conftest.py` fixtures are imported by the entire test tree — CLEAN-02's blast radius is the
  whole suite, hence the D-08 full-suite green gate.

</code_context>

<specifics>
## Specific Ideas

- The `asyncio.gather` follow-up is the audit's *literal* recommended next step before any
  denormalization — CLEAN-01 is explicitly the gate that decides DENORM-01's fate (D-05).
- D-07's transactional-rollback fixture is the well-known SQLAlchemy "run each test in an
  external transaction" recipe; the non-obvious part for this codebase is the
  restart-SAVEPOINT-on-commit hook required by the `get_session`-never-commits pattern.

</specifics>

<deferred>
## Deferred Ideas

None new — discussion stayed within phase scope. The out-of-scope items (DENORM-01,
P85 WR-01..04, P81 WR-01/02, 83-06, live-corpus deploy rehearsal) are pre-existing deferrals
recorded in the ROADMAP "Out of scope" line and MEMORY; they are NOT to be pulled into Phase 92.

</deferred>

---

*Phase: 92-milestone-close-tech-debt-cleanup*
*Context gathered: 2026-07-13*
