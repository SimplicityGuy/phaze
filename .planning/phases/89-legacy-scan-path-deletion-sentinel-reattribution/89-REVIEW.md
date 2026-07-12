---
phase: 89-legacy-scan-path-deletion-sentinel-reattribution
reviewed: 2026-07-11T00:00:00Z
depth: standard
files_reviewed: 8
files_reviewed_list:
  - alembic/versions/038_retire_legacy_sentinel.py
  - src/phaze/main.py
  - src/phaze/models/file.py
  - src/phaze/models/scan_batch.py
  - tests/integration/test_migrations/conftest.py
  - tests/integration/test_migrations/test_012_upgrade.py
  - tests/integration/test_migrations/test_013_upgrade.py
  - tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py
findings:
  critical: 2
  warning: 3
  info: 2
  total: 7
status: resolved
---

## Disposition (orchestrator, 2026-07-11)

- **CR-01** (composite-UQ reattribution collision) — **FIXED**. Added a pre-flight guard
  (step 1.5) in `038_retire_legacy_sentinel.py` that aborts with clear operator guidance +
  rollback when the target already owns a file at a legacy file's `original_path`, plus a
  mutation-verified regression test (Scenario 4b). Commit `fix(89-02): guard migration 038
  against target original_path collision (CR-01)`.
- **CR-02** (038 aborts `upgrade head` on a fresh DB with no fileserver) — **ACCEPTED as
  designed**. The operator made an explicit, informed decision to keep migration 038 strict
  (locked decision D-01: 0 fileservers → abort) and reconcile only the test layer (seed a
  fileserver in `migrated_engine`, pin sentinel-dependent tests to revision 037 via
  `pre_retire_engine`). Fresh-deploy / auto-migrate boot on an agent-less DB remains a known,
  accepted constraint. Not changed.
- **WR-01/02/03, IN-01/02** — advisory; left as-is for a future polish pass (WR-02 anti-f-string
  guard tightening, IN-01 `transaction_per_migration` docstring accuracy). None affect
  correctness of the shipped migration.

# Phase 89: Code Review Report

**Reviewed:** 2026-07-11
**Depth:** standard
**Files Reviewed:** 8
**Status:** issues_found

## Summary

Phase 89 deletes the orphaned legacy scan endpoint (`routers/scan.py`, `schemas/scan.py`,
`services/ingestion.py`), drops the `agent_id` model default on `FileRecord`/`ScanBatch`, and adds
data-only migration 038 which reattributes `legacy-application-server`-owned `files`/`scan_batches`
rows to a real fileserver agent, then deletes the sentinel.

The source deletions are clean: no live code imports the removed modules (only stale docstring
comments remain), and every production insert path (`agent_files.py`, `pipeline_scans.py`,
`agent_bootstrap.py`) stamps `agent_id` explicitly, so dropping the ORM default is safe. The
RESTRICT-FK satisfiability argument holds at the schema level — only `files` and `scan_batches` carry
FKs to `agents.id`, and both are reattributed before the sentinel DELETE. SQL parameterization is
sound: the operator override and target id are passed via `bindparams`, never f-stringed.

However, migration 038 has two correctness defects that the specially-modified test conftest actively
hides. First, the reattribution `UPDATE` can violate the `uq_files_agent_id_original_path` composite
unique index in a realistic production state (a real fileserver having re-scanned paths the legacy
agent originally owned) — a scenario that migration 013's own D-16 downgrade guard explicitly
acknowledges as real. Second, and more universally, 038 unconditionally requires a non-revoked
fileserver to exist and aborts if none does — which breaks `alembic upgrade head` (and the
auto-migrate startup path) on every fresh database, including the documented local-UAT boot recipe.
The conftest's `_seed_fileserver` hook masks exactly this in the test harness.

## Critical Issues

### CR-01: Reattribution UPDATE can violate `uq_files_agent_id_original_path`, aborting the migration with an opaque IntegrityError

**File:** `alembic/versions/038_retire_legacy_sentinel.py:84,129`
**Issue:**
`_REATTRIBUTE_FILES = "UPDATE files SET agent_id = :target WHERE agent_id = 'legacy-application-server'"`
re-points every legacy-owned `files` row to the target fileserver. But `files` carries a composite
unique index `uq_files_agent_id_original_path` on `(agent_id, original_path)` (models/file.py:99,
migration 013). If the target fileserver already owns a row at the same `original_path` as any
legacy-owned row, the UPDATE produces a duplicate `(target, original_path)` and raises `IntegrityError`.

This is not a hypothetical edge case. The `legacy-application-server` sentinel represents the pre-agent
v3.0 world where files were scanned from `/data/music`. A real fileserver agent (e.g. `nox`) that later
re-scanned the same tree creates NEW `files` rows with identical `original_path` under its own
`agent_id` — this is precisely what the Phase-013 composite UQ was designed to allow, and migration
013's downgrade guard (D-16, `013:47-55`) exists specifically because "the same original_path now lives
under multiple agents" is a known real state. Reattributing legacy → target in that state collides.

Consequences: the migration aborts mid-transaction with a raw `IntegrityError` (not the clean
`RuntimeError` operator guidance provided for the 0/>1-fileserver cases), rolls back (no data loss), and
blocks the deployment with no in-code remediation path. The migration's central contract — "reattribute
every historical legacy-owned files row" — silently cannot be met whenever the target has overlapping
paths. The 038 test suite never seeds a target-owned file at the same `original_path` as a legacy file,
so this path is entirely uncovered.

**Fix:** Detect and handle the collision explicitly before the bulk UPDATE. Either (a) pre-scan for
overlapping `(original_path)` between legacy-owned and target-owned rows and abort with a clear
`RuntimeError` naming the collisions and an operator remediation step (mirroring 013's D-16 guard), or
(b) resolve them deterministically (e.g. delete the legacy duplicate when a target row already covers the
same `original_path`, per an explicit locked decision). Add a regression test seeding a target file and a
legacy file at the same `original_path`:
```python
# guard example (abort loudly instead of opaque IntegrityError)
collisions = bind.execute(sa.text(
    "SELECT l.original_path FROM files l JOIN files t "
    "ON t.original_path = l.original_path AND t.agent_id = :target "
    "WHERE l.agent_id = 'legacy-application-server' LIMIT 5"
).bindparams(target=target)).scalars().all()
if collisions:
    raise RuntimeError(
        f"Cannot reattribute: target {target!r} already owns files at legacy paths {collisions!r}. "
        "Resolve these duplicates before retrying."
    )
```

### CR-02: 038 aborts `alembic upgrade head` on every fresh database (breaks fresh deploys, UAT, and auto-migrate startup)

**File:** `alembic/versions/038_retire_legacy_sentinel.py:112-114,121-124`
**Issue:**
`_resolve_target` aborts with `RuntimeError("No non-revoked fileserver agent exists; ...")` whenever
zero non-revoked `kind='fileserver'` agents exist, and `upgrade()` calls `_resolve_target(bind)`
unconditionally — before checking whether there is anything to reattribute.

Fileserver agents are runtime data, self-registered by agent containers, and are NEVER migration-seeded.
Migration 012, by contrast, UNCONDITIONALLY seeds the `legacy-application-server` agent row and its LIVE
`<watcher>` scan_batch on every DB (`012:55-61,92-101`). Therefore, on any fresh database, revision 037
always has the sentinel to retire but no fileserver to reattribute to — so `alembic upgrade head` reaches
038 and hard-aborts, even though there are zero legacy-owned `files` and the only real work is deleting
the sentinel + its zero-value live batch.

This breaks:
- Every fresh deployment / new environment schema provisioning via `alembic upgrade head`.
- The `main.py` lifespan auto-migrate path (`main.py:87` `await run_migrations()` runs `upgrade head` on
  startup, gated by `settings.auto_migrate`) — a fresh stack with `auto_migrate=true` crashes on boot
  before `ensure_dev_agent` (line 98) ever runs, and the dev agent isn't a `kind='fileserver'` anyway.
- The documented local-UAT boot recipe (`PHAZE_AUTO_MIGRATE` on a fresh `phaze_uat` DB).
- The auto-migrate path additionally cannot supply `-x reattribute_to=<id>` (see WR-01), so there is no
  escape hatch on that path even when a fileserver later exists.

The test conftest confirms the defect in its own comment: "a bare `upgrade head` on a fresh DB aborts at
038. `migrated_engine` seeds exactly one non-revoked fileserver between the 037 and 038 upgrades so
auto-detect resolves it" (`conftest.py:41-46,106-120,159-162`). That seed exists ONLY in the test
harness; production fresh deploys have no equivalent, so the tests are green while `upgrade head` is
non-viable on a fresh DB.

**Fix:** Short-circuit when there is nothing to reattribute so a fresh DB (and any DB with no
legacy-owned data beyond the sentinel) can pass. Resolve the target only when reattribution is actually
required; when zero legacy-owned `files`/`scan_batches` rows exist, delete the sentinel's live watcher
batch + the sentinel agent row directly without demanding a fileserver:
```python
def upgrade() -> None:
    bind = op.get_bind()
    # Delete the zero-value legacy live watcher batch regardless (012 seeds it unconditionally).
    bind.execute(sa.text(_DELETE_LEGACY_LIVE_BATCH))
    legacy_rows = bind.execute(sa.text(_COUNT_REMAINING)).scalar_one()
    if legacy_rows:
        target = _resolve_target(bind)  # only require a fileserver when there is real data to move
        bind.execute(sa.text(_REATTRIBUTE_FILES).bindparams(target=target))
        bind.execute(sa.text(_REATTRIBUTE_SCAN_BATCHES).bindparams(target=target))
        remaining = bind.execute(sa.text(_COUNT_REMAINING)).scalar_one()
        if remaining != 0:
            raise RuntimeError(f"Reattribution incomplete: {remaining} legacy-owned rows remain.")
    bind.execute(sa.text(_DELETE_SENTINEL))
```
Add a test that runs `upgrade head` on a fresh DB with NO seeded fileserver and asserts it succeeds and
deletes the sentinel (the scenario the current `_seed_fileserver` hook hides).

## Warnings

### WR-01: `-x reattribute_to` override is unreachable via the auto-migrate startup path

**File:** `src/phaze/database.py:59-73`, `alembic/versions/038_retire_legacy_sentinel.py:105`
**Issue:** `_resolve_target` reads the operator override via `context.get_x_argument(...)`, which is only
populated from `config.cmd_opts.x`. The app's auto-migrate path (`_run_upgrade_head_sync` →
`command.upgrade(cfg, "head")`) never sets `cmd_opts`, so the override is always empty there. On a
deployment with more than one non-revoked fileserver and `auto_migrate=true`, 038 aborts with the ">1
fileserver, pass -x" guidance — but the auto-migrate path has no way to pass `-x`, so the operator must
disable `auto_migrate` and run the migration manually. This trap is undocumented in the code paths that
would hit it.
**Fix:** Support an env-var fallback for the target when running under auto-migrate (e.g. read
`PHAZE_REATTRIBUTE_TO` in `_resolve_target` when no `-x` is present), or document in `run_migrations`
that operators must run 038 manually with `-x` on multi-fileserver deployments. If CR-02's short-circuit
is adopted, the practical blast radius shrinks to the genuinely-ambiguous multi-fileserver-with-data case.

### WR-02: Anti-f-string injection guard in the 038 test is toothless for single-line f-strings

**File:** `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py:117`
**Issue:** `assert "f'''" not in body and 'f"""' not in body` only bans triple-quoted f-strings. A
single-line f-stringed SQL statement (e.g. `f"UPDATE files SET agent_id = '{target}'"`) would pass this
guard unchanged, so it does not actually protect against the T-89-02-01 injection surface it claims to
cover. (The migration already uses single-line `f"..."` for its `RuntimeError` messages, which is why a
blanket `"f\"" not in body` ban was not used — but that means the guard cannot distinguish safe
error-message f-strings from an unsafe SQL f-string.) Per the project's own "mutation-test your guard
tests" convention, this assertion would not catch a regression.
**Fix:** Make the guard positive/structural instead of a substring blocklist — e.g. assert that each of
the reattribution SQL constants contains `:target` and does NOT contain `{target}`/`{override}`, or
parse the module AST and assert the SQL statement strings are not `JoinedStr` (f-string) nodes.

### WR-03: Reattribution UPDATEs are unbounded — a partial-failure re-run has no idempotency guard

**File:** `alembic/versions/038_retire_legacy_sentinel.py:129-130`
**Issue:** The docstring notes "~11,428 rows ... sub-second, no batching needed." That may hold, but the
migration offers no re-entrancy story beyond full-transaction rollback: if the process is killed after the
`files` UPDATE commits (it won't under one transaction, but the design leans entirely on
single-transaction semantics that the docstring mis-attributes to `transaction_per_migration` — see
IN-01). If a future change ever splits this into multiple transactions or adds batching, the `WHERE
agent_id = 'legacy-application-server'` predicate is naturally idempotent for `files`/`scan_batches`, but
the sentinel DELETE is gated only by the in-run COUNT. This is currently safe but fragile; the
correctness rests on an env.py transaction mode the docstring describes incorrectly.
**Fix:** Either add an explicit `with bind.begin():`-style assertion of atomicity, or correct the
docstring (IN-01) and add a comment that the whole body MUST remain a single statement group. Low
priority given current single-migration atomicity, but the mis-stated transaction mode makes the safety
argument non-auditable.

## Info

### IN-01: Docstring cites `transaction_per_migration` but `env.py` does not configure it

**File:** `alembic/versions/038_retire_legacy_sentinel.py:19-21,44`
**Issue:** The module docstring repeatedly asserts rollback safety "under Alembic's
`transaction_per_migration`". However `alembic/env.py:59-68` (`do_run_migrations`) calls
`context.configure(...)` without `transaction_per_migration=True`, so the default mode (one transaction
wrapping ALL pending migrations) is in effect, not per-migration transactions. The rollback guarantee
still holds (a `raise` rolls the enclosing transaction back, and default mode is at least as atomic), so
this is a documentation inaccuracy rather than a behavioral bug — but it makes the migration's own safety
rationale unverifiable against the actual env configuration.
**Fix:** Update the docstring to state the actual mode ("Alembic's default single-transaction online
migration wrapping in `env.py:do_run_migrations`"), or set `transaction_per_migration=True` in `env.py`
if per-migration isolation is intended.

### IN-02: Stale docstring/comment references to deleted `services/ingestion.py`

**File:** `src/phaze/routers/agent_files.py:124,133`, `src/phaze/tasks/scan.py:13,18,72,74,181`
**Issue:** Phase 89 deleted `src/phaze/services/ingestion.py`, but several comments still reference it
(e.g. "Mirrors services/ingestion.py:103-117", "services.ingestion transitively imports phaze.models").
These are comments only — no live import remains — so there is no functional impact, but the line-number
citations now point at a nonexistent file and will mislead future readers.
**Fix:** Update or remove the `services/ingestion.py` references in these comments (the mirrored logic now
lives inline in `agent_files.py` / `tasks/scan.py`).

---

_Reviewed: 2026-07-11_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
