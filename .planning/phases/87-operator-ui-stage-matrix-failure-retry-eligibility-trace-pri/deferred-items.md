# Phase 87 — Deferred Items

Out-of-scope discoveries logged during execution (SCOPE BOUNDARY). Not fixed by the discovering plan.

## From Plan 02 (Wave 2)

- **RESOLVED (orchestrator, mid-phase): `tests/integration/test_drain_double_dispatch.py` — 3 setup errors: `ModuleNotFoundError: No module named 'psycopg2'`.**
  - Discovered while running clause-consumer no-regression tests (Plan 02, Task 2).
  - Corrected root cause: NOT a SAQ `scoped_runner` fixture. The three tests consume the shared
    `async_engine` fixture (`tests/conftest.py`), which feeds `TEST_DATABASE_URL` straight to
    `create_async_engine`. When an operator exports a **bare** `postgresql://` DSN (the natural form —
    it matches `PHAZE_QUEUE_URL`), SQLAlchemy resolves its default **psycopg2** sync dialect, which the
    async-only stack does not install → every DB-fixture test dies at setup. Reproduced deterministically
    with `TEST_DATABASE_URL=postgresql://…`.
  - NOT caused by Plan 02: changes were purely additive ORM `ColumnElement` builders + a new `Status`
    member (asyncpg path only). Latent footgun in shared test infra.
  - Fix applied: `_coerce_async_dsn()` in `tests/conftest.py` normalizes bare `postgresql://`,
    `postgresql+psycopg2://`, and `postgresql+psycopg://` DSNs to `postgresql+asyncpg://` before the
    engine is built (only the leading driver token is rewritten). Rejected the "add psycopg2-binary"
    option — it violates the project's async-only driver rule (CLAUDE.md: psycopg2 is a sync driver to
    avoid). Regression guard: `tests/shared/test_conftest_dsn_coercion.py`. Verified the drain suite now
    passes under a bare `postgresql://` DSN.

## From Plan 03 (Wave 3)

- **RESOLVED (orchestrator, mid-phase) — Recovery re-enqueues a force-SKIPPED fingerprint file (behavior 5 gap for the fingerprint stage).**
  - Fix applied: `_build_done_sets` now derives `fingerprint_done` from
    `or_(done_clause(Stage.FINGERPRINT), skipped_clause(Stage.FINGERPRINT))` in `src/phaze/tasks/reenqueue.py`
    (a FAILED-but-not-skipped fingerprint still auto-retries; a force-SKIPPED one is excluded). Docstrings
    updated at every anchor that claimed "done_clause(FINGERPRINT) ONLY". The strict-xfail tripwire
    `test_skipped_fingerprint_row_is_excluded_from_recovery` was converted to a plain passing guard; the
    full recovery suite is green (54 passed) and the guard was mutation-verified (stripping the
    `skipped_clause` disjunct re-RED'd it with `fingerprint_file reenqueued: 1`).
  - Original finding below (kept for the record):
  - Discovered while writing the Task-2 recovery guard (`tests/analyze/tasks/test_recovery.py`).
  - Root cause: `phaze.tasks.reenqueue._build_done_sets` derives `fingerprint_done` from
    `done_clause(Stage.FINGERPRINT)` **only** (deliberately, so a FAILED fingerprint auto-retries —
    `FAILURE_IS_TERMINAL[fingerprint] is False`). It never consults `skipped_clause(FINGERPRINT)`, so a
    file the operator force-skipped for fingerprint — whose `stage_skip` marker makes it `skipped` and
    therefore domain-complete everywhere else — is NOT in `fingerprint_done`. If its `fingerprint_file`
    ledger row survives a crash/restart (the exact durability case recovery exists for) and its saq_jobs
    key is not live, `recover_orphaned_work` re-enqueues it, re-driving a stage the operator explicitly
    skipped. analyze/metadata do NOT have this gap: they read `domain_completed_clause`, into which Plan
    02 threaded `skipped_clause` as an unconditional disjunct.
  - Empirically confirmed (`is_domain_completed` returns `False` for a skipped fingerprint file; `True`
    for skipped analyze/metadata).
  - Why deferred (not fixed inline): Plan 03 is TESTS-ONLY and runs as a **parallel** executor whose
    file ownership excludes `src/phaze/tasks/reenqueue.py` (a shared source file — plan 04 runs
    concurrently). Touching it would violate the isolation contract.
  - Proposed fix (1 line, when a source-owning plan can take it): derive `fingerprint_done` from
    `or_(done_clause(Stage.FINGERPRINT), skipped_clause(Stage.FINGERPRINT))` in `_build_done_sets` (a
    FAILED-but-not-skipped fingerprint still auto-retries; a SKIPPED one is excluded). Do NOT switch it
    to `domain_completed_clause(FINGERPRINT)` — that would collapse to the same `or_(done, skipped)`
    here (fingerprint has no terminal-failure disjunct) but couples recovery to the terminality axis and
    obscures the FAIL-04 auto-retry intent.
  - Tracked by a **strict-xfail regression guard**:
    `tests/analyze/tasks/test_recovery.py::test_skipped_fingerprint_row_is_excluded_from_recovery`. It
    asserts the DESIRED behavior and currently xfails; when the fix lands it XPASSes and (strict) turns
    the suite RED — remove the marker then.

---

## [87-08] RESOLVED (orchestrator, mid-phase) — Pre-existing: `x-cloak` has no backing CSS rule (app-wide, cosmetic)

- **Fix applied:** added `[x-cloak] { display: none !important; }` to `assets/src/app.css` (after the
  `htmx-indicator` rules) and rebuilt via `just tailwind` — verified the rule compiles into the
  gitignored `src/phaze/static/css/app.css` (`[x-cloak]{display:none!important}`). Regression guard:
  `tests/shared/test_x_cloak_css_rule.py` asserts the source rule exists (the compiled output is
  gitignored + rebuilt at image-build, so the source is the durable artifact). App-wide fix: every
  existing `x-cloak` (theme toggle, header, cmdk modal, record host, agents table) now cloaks correctly.
- **Original finding below (kept for the record):**

- **Found during:** 87-08 Task 2 (rail orphan badge + priority/pause controls).
- **Issue:** Alpine v3 does NOT auto-inject the `[x-cloak]{display:none}` rule — the app must define
  it. Neither `assets/src/app.css` nor the compiled `src/phaze/static/css/app.css` defines it, so every
  `x-cloak` in the codebase (base.html theme-toggle SVGs, header, cmdk_modal, record_host, agents_table,
  and now the rail orphan badge / Resume / Paused caption) is INERT. Elements meant to be hidden-until-
  Alpine-inits instead flash their fallback content for a few ms on first paint (e.g. a brief amber "0"
  orphan pill, or a brief "Resume" button, before the store default hides them via `x-show`).
- **Blast radius:** cosmetic only, sub-100ms, on initial page load; no functional impact (the store
  defaults to 0 / not-paused, so `x-show` hides them the instant Alpine initializes).
- **Why deferred (not fixed inline):** the fix is a one-line rule in `assets/src/app.css`
  (`[x-cloak]{display:none !important;}`) — NOT a declared file for this plan, and the compiled CSS is
  gitignored + rebuilt at image-build (`just tailwind`), so verifying the fix requires a CSS rebuild
  outside this plan's scope. It is a pre-existing, app-wide latent issue, not introduced by 87-08.
- **Proposed fix (when a CSS-owning change can take it):** add `[x-cloak] { display: none !important; }`
  to `assets/src/app.css` (after the `htmx-indicator` rules) and rebuild via `just tailwind`.
