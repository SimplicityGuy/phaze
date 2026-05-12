# Phase 26 — Deferred Items (out-of-scope discoveries during execution)

These issues were discovered during phase 26 plan execution but are unrelated
to the in-flight plan's scope. Each item is logged here per the GSD executor
scope-boundary rule rather than auto-fixed.

---

## DEF-26-08-01: `test_tags.py` UniqueViolation race in fixture setup

**Discovered:** Plan 26-08 (during full test-suite run)
**Reproducer:** `uv run pytest tests/test_routers/test_tags.py -x --no-cov`
**Error:**
```
asyncpg.exceptions.UniqueViolationError:
  duplicate key value violates unique constraint "pg_type_typname_nsp_index"
```

**Root cause hypothesis:** The `async_engine` conftest fixture seeds an Agent row
with `id="legacy-application-server"` after `Base.metadata.create_all`. When
`test_tags.py` runs against a shared local Postgres database that still has
leftover types (e.g., enum types from a previous interrupted run), creating
duplicate type names races. This is independent of any Phase 26 router
work — confirmed by `git stash`-ing my changes and reproducing the same
failure on a clean tree.

**Why not auto-fixed:** Outside Plan 26-08's `files_modified` scope
(`tests/test_routers/test_agent_proposals.py`, `src/phaze/routers/agent_proposals.py`).
Touching `tests/conftest.py` or the migrations layer would expand scope.

**Suggested resolution path:** Investigate whether `Base.metadata.drop_all`
properly drops custom enum types in `async_engine` teardown; consider running
each test module against a uniquely-named test schema; or wrap `create_all`
in a `DROP TYPE IF EXISTS … CASCADE` preamble. Future hardening phase or
ad-hoc CI fix.
