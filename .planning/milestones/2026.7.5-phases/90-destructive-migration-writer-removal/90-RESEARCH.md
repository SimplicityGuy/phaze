# Phase 90: Destructive Migration & Writer Removal - Research

**Researched:** 2026-07-12
**Domain:** Live Postgres data-migration (destructive DDL) + linear-enum retirement in a running FastAPI/SQLAlchemy/Alembic pipeline
**Confidence:** HIGH (codebase-verified inventories; Postgres DDL-lock semantics cross-checked with docs)

## Summary

This phase drops `files.state` (a plain `String(30)`, **not** a native PG enum — confirmed `models/file.py:86`), drops `ix_files_state`, deletes the `FileState` Python class, and removes every surviving `state=` writer. The next Alembic revision after `038` is **`039`** (`alembic/versions/` head verified). Because `state` is a String column there is **no `DROP TYPE`** — the destructive DDL is exactly `op.drop_index('ix_files_state')` + `op.drop_column('files','state')`, and Postgres auto-drops the index with the column anyway (being explicit is cleaner and mirrors the ORM `__table_args__` edit). The derivation layer (Phase 78 `services/stage_status.py` clause builders + `enums/stage.py`) and the sidecar/marker tables (`cloud_job`, `analysis.failed_at`, `dedup_resolution`) are already in place and are the derived sources every converted reader targets.

**The single most important finding overrides a premise of the locked plan.** CONTEXT's `SCOPE DISCOVERY` note and locked decisions **D-01/D-02** assert that PR-1 is "pure writer removal … behavior-preserving (no reader consumes state)." Research falsifies this: there are **~11 live `files.state` readers still in the tree** (not the "dashboard + analyze workspace" pair the note implies), and **several are read-coupled to the very writers PR-1 removes** — e.g. removing the `AWAITING_CLOUD` writer (`backends.py:124`) silently breaks the live `held_files` reader (`routers/pipeline.py:1040`) while the column still exists. Additionally the writer count is **~17, not ~9** — the note missed every `update(...).values(state=...)` form and the INSERT-time stamp. Section "User Constraints" and "Common Pitfalls" carry the full corrected inventory; **Pitfall 1 recommends the planner escalate the PR ordering (readers-first) before writing plans.**

**Primary recommendation:** Re-sequence to **readers-first**: (PR-A) convert all live `files.state` readers to derived sources while the column is intact → (PR-B) remove all writers → (PR-C) `039` destructive migration with a `lock_timeout`+savepoint-retry DDL wrapper, a `files_state_archive` snapshot, a shadow-compare-invariant self-guard (inline sync SQL, gated only when data exists), and a best-effort derived `downgrade()`. Every claim below is codebase-verified unless tagged otherwise.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Ship Phase 90 as **THREE sequential PRs**, each independently green & shippable, destructive change isolated last: **PR-1** pure writer removal (delete only the `.state=` write statements + now-unused `FileState` imports/dead branches; column/index/ORM mapping/enum stay intact); **PR-2** reader cutover (convert surviving live `FileRecord.state` readers to derived sources while the column is present); **PR-3** destructive (`039` migration + `op.drop_index` + `op.drop_column` + delete `FileState` class + final cleanup).
- **D-02:** All reader cutovers (PR-2) MUST land before the destructive drop (PR-3). Any surviving `FileRecord.state` read is a hard blocker for `op.drop_column`.
- **D-03:** `downgrade()` is **best-effort backfill** (not `NotImplementedError`). Recreate the column + `ix_files_state`, then backfill a representative `FileState` per file from the derived markers.
- **D-04:** Collapse precedence = **furthest-along linear pipeline stage** (walk `DISCOVERED < METADATA_EXTRACTED < FINGERPRINTED < ANALYZED < PROPOSAL_GENERATED < APPROVED/EXECUTED/…`, pick most-advanced reached).
- **D-05:** **Durable markers override the ladder; transients are lost.** analyze-failure marker → `ANALYSIS_FAILED`; dedup marker → `DUPLICATE_RESOLVED`; `proposals.status='rejected'` → `REJECTED`. Transients (`LOCAL_ANALYZING`, `PUSHING`, `PUSHED`, `AWAITING_CLOUD`, rollback-`FINGERPRINTED`) are unrecoverable → collapse to nearest durable stage, each enumerated as lossy in the docstring. Round-trip test asserts **only** durable/reconstructable cases.
- **D-06:** `039.upgrade()` **self-guards, but only when data exists.** `RAISE`s (aborts txn) on mid-flight rows (`files.state IN ('pushing','uploading')` OR non-terminal `cloud_job`) OR failed shadow-compare implication invariants. On empty/fresh DB it proceeds cleanly — **explicitly avoiding the Phase-89 `038` fresh-DB-abort footgun (CR-02).**
- **D-07:** The shadow-compare precondition is re-expressed as **inline sync SQL inside `upgrade()`** (`op.get_bind().execute(...)`), **NOT** an import of `services/shadow_compare.py`. A versioned migration is frozen-in-time and must not couple to mutable app code (stays sync, never references `saq_jobs`). Accepts SQL duplication as the cost of decoupling.
- **D-08:** Type checker is the **primary** anti-drift guard. Add **ONE thin source-grep test** forbidding `FileState` / `files.state` / `.state =` from reappearing in `src/`, and **mutation-test it** (add a fake `.state=`, watch RED, restore). No full behavioral schema-absence suite.

### Claude's Discretion
- `039` revision number is mechanically next after `038` → **`039`** (verified).
- Batching/lock strategy for the `downgrade()` backfill `UPDATE` over the ~11,428-file prod corpus.
- Exact abort-message wording for the mid-flight / shadow-compare-fail guard; the docstring prose enumerating lossy downgrade cases.
- Precise PR-2 reader-conversion boundaries (delete-as-dead vs convert-to-derived), provided every live reader is cut before the drop and each PR is independently green.

### Deferred Ideas (OUT OF SCOPE)
None. (Reader-cutover expansion is NOT scope creep — converting surviving `files.state` readers is a mandatory prerequisite for MIG-04's column drop.)
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| **MIG-04** | Destructive migration lands last (after shadow-compare green + cloud-push lanes drained): drop `ix_files_state`, drop `files.state`, delete `FileState`, remove remaining `.state=` writers; `downgrade()` documents enum reconstruction from derived sources + lossiness. | Complete reader/writer inventory (Standard Stack + Pitfalls); `039` DDL + `lock_timeout` retry recipe (Pattern 1); archival + delta-backfill design (Pattern 2); guard SQL from shadow-compare invariants (Pattern 3); `downgrade()` reconstruction CASE ladder (Pattern 4); rehearsal recipe (Pattern 5). MIG-02 (Phase 79 shadow-compare) is the green gate this depends on. MIG-03 (Phase 77) already made rescan-wipe structurally impossible. |
</phase_requirements>

## Project Constraints (from CLAUDE.md + design §8)

- Python **3.14**, **uv only** — every command `uv run …`; never bare `pip`/`python`/`pytest`/`mypy`.
- **Sync migrations** (`def upgrade()`, plain `op.*`; only `env.py` is async), 3-digit zero-padded string revisions, **mirrored `downgrade()`**, **integration test per migration** in `tests/integration/test_migrations/`.
- **Migrations NEVER reference `saq_jobs`** (SAQ-owned; 020/031/032/038 banner). This binds `039`'s guard SQL — it may read `files`, `cloud_job`, `analysis`, `dedup_resolution`, `proposals`, `metadata`, `execution_log`; it may NOT read `saq_jobs` or `scheduling_ledger`-derived in-flight (the ledger read is app-layer only).
- `ruff` clean (line length 150), `mypy` strict clean (excludes `tests/`), **90% coverage** floor, `T20` print ban outside CLI/tests, `S`/bandit (`-s B608`) — the migration's SQL must be parameterized `sa.text(...).bindparams(...)`, never f-stringed (038 discipline).
- **Per-bucket test isolation:** every new test passes via `just test-bucket <bucket>` in isolation. Migration tests live in the `integration` bucket and need **`MIGRATIONS_TEST_DATABASE_URL` pointed at `:5433`** — `just test-bucket` does NOT export it (documented footgun in `test_migration_038`'s header; export explicitly or the harness silently hits the wrong `:5432` DB).
- **PR per phase / worktree per phase / never push to `main` / never `--no-verify`.**
- **`branching_strategy=none`** for this repo (memory: GSD-vs-Orca worktree) — do not let GSD recreate `gsd/phase-*` branches.

## Standard Stack

No new dependencies (design §8 non-goal: "no new dependencies"). Everything needed is in-tree and verified:

### Derived-source clause builders (PR-2 readers convert TO these) — `src/phaze/services/stage_status.py`
| Builder | Replaces legacy state | Body (verified) |
|---------|----------------------|-----------------|
| `done_clause(Stage.ANALYZE)` | `state == ANALYZED` | `exists(analysis WHERE file_id AND analysis_completed_at IS NOT NULL)` |
| `failed_clause(Stage.ANALYZE)` | `state == ANALYSIS_FAILED` | `exists(analysis WHERE file_id AND failed_at IS NOT NULL)` |
| `done_clause(Stage.METADATA)` | `state == METADATA_EXTRACTED` | `exists(metadata WHERE file_id AND failed_at IS NULL)` |
| `done_clause(Stage.FINGERPRINT)` | `state == FINGERPRINTED` (see caveat) | `exists(fingerprint_results WHERE file_id AND status IN ('success','completed'))` |
| `done_clause(Stage.PROPOSE)` | `state == PROPOSAL_GENERATED` | `exists(proposals WHERE file_id)` |
| `dedup_resolved_clause()` | `state == DUPLICATE_RESOLVED` | `exists(dedup_resolution WHERE file_id)` |
| `awaiting_candidate_clause()` | `state == AWAITING_CLOUD` (drain-scoped) | `cloud_job.status='awaiting' ∧ ~inflight(ANALYZE) ∧ ~domain_completed(ANALYZE)` |
| `eligible_clause(Stage.ANALYZE)` | analyze pending set | already used by `get_discovered_files_with_duration` (cut over) |
| `applied_clause()` / `is_applied()` | `state == EXECUTED` | `exists(proposals WHERE status='executed')` |

**Caveat (design §6.1, verified):** `FINGERPRINTED`'s sole writer is `retry_analysis_failed` rolling a file *back* out of `ANALYSIS_FAILED`; such files may have no `fingerprint_results` success. Under derivation they correctly become `fingerprint: not_started`. Shadow-compare soft-allowlists `fingerprinted` for exactly this reason. This is documented divergence, not a bug.

### DB-free resolver / eligibility — `src/phaze/enums/stage.py`
`Stage`, `Status`, `resolve_status`, `eligible`, `domain_completed`, `ELIGIBILITY_DAG`, `FAILURE_IS_TERMINAL`, `ELIGIBLE_AFTER_FAILURE`. The SQL twins in `stage_status.py` are drift-locked to these by `tests/integration/test_stage_status_equivalence.py`.

### Migration template — `alembic/versions/038_retire_legacy_sentinel.py`
The canonical precedent for: raw `sa.text` (no model imports → immune to model drift, exactly what D-07 wants), `bindparams` parameterization, raise-to-rollback guard, `-x` override via `context.get_x_argument(as_dictionary=True)`, and the "empty autogenerate diff" contract test.

### Shadow-compare invariants — `src/phaze/services/shadow_compare.py`
The `INVARIANTS` registry is the exact source to transcribe into `039`'s inline guard SQL (D-07). Hard (gating) invariants: `metadata_extracted, analyzed, analysis_failed, proposal_generated, awaiting_cloud, pushing, pushed, duplicate_resolved, approved, rejected, executed, failed, moved, unchanged`. Soft (never gate): `fingerprinted, local_analyzing`.

**Installation:** none. **Version verification:** N/A (no package changes).

## Package Legitimacy Audit

Not applicable — this phase installs **no external packages** (design §8 non-goal: zero new dependencies). No slopcheck run required.

## Architecture Patterns

### Pattern 1: `lock_timeout` + savepoint-retry DDL wrapper (success-criterion 1)

**What:** `DROP COLUMN`/`DROP INDEX` take `ACCESS EXCLUSIVE` on `files`. That lock is **catalog-only and fast to *hold*** (Postgres marks the column invisible; **no table rewrite** — `[VERIFIED: dev.to/mickelsamuel ALTER-lock table + leopard.in.ua]`). The danger is *acquiring* it: if the DDL queues behind the live 5s `/pipeline/stats` `ACCESS SHARE` readers, it in turn blocks every subsequent SELECT behind it in the lock queue → dashboard stall. The fix is a short `lock_timeout` so the DDL **aborts-and-retries** instead of queuing.

**When to use:** the destructive DDL in `039.upgrade()`.

**Recipe (recommended — savepoint per attempt so `SET LOCAL` is correctly scoped and a lock-timeout error only rolls back the attempt, not the whole migration):**
```python
# Source: pattern adapted from GitLab with_lock_retries + Postgres lock_timeout docs [CITED]
import time
from sqlalchemy.exc import OperationalError
LOCK_TIMEOUT = "2s"          # < the 5s dashboard poll, so a queued reader can't hold us long
MAX_ATTEMPTS, BACKOFF = 6, [0.5, 1, 2, 4, 8]   # ~15s worst case; DROP is instantaneous once acquired
bind = op.get_bind()
for attempt in range(MAX_ATTEMPTS):
    try:
        with bind.begin_nested():                       # SAVEPOINT; SET LOCAL reverts on rollback
            bind.execute(sa.text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))  # literal is a fixed const, not user input
            op.drop_index("ix_files_state", table_name="files")
            op.drop_column("files", "state")
        break
    except OperationalError:                             # LockNotAvailable (55P03) / QueryCanceled
        if attempt == MAX_ATTEMPTS - 1:
            raise
        time.sleep(BACKOFF[attempt])
```
**Notes / open items for the planner:**
- **`SET LOCAL` scope is correct** here: it lasts to the end of the current (sub)transaction, so wrapping in `begin_nested()` bounds it to the attempt. `[VERIFIED: postgres SET docs semantics + codebase begin_nested idiom]`.
- **Verify `env.py`'s transaction mode.** `alembic/env.py` does not set `transaction_per_migration` (grep: only two `context.configure` calls). The savepoint-retry works under either default, but the planner must confirm the outer transaction isn't left aborted between attempts (fresh savepoint each attempt handles this). `[ASSUMED — needs a quick env.py read at plan time]`.
- Dropping the index explicitly is belt-and-suspenders: `DROP COLUMN` cascade-drops `ix_files_state` automatically (single-column index) `[VERIFIED: DROP COLUMN auto-removes column-only indexes]`, but the explicit `drop_index` keeps the migration self-documenting and pairs 1:1 with removing `Index("ix_files_state", "state")` from `models/file.py:97`.
- Deploy runs during the **`--profile drain`** quiesce window (cloud-push lanes stopped), so live write contention is already minimal — the retry is defense-in-depth, not the primary safety.

### Pattern 2: Archival + delta-backfill-since-032 (success-criterion 1)

**"Archive `files.state`" concretely** = create and populate a `files_state_archive(file_id UUID PK, state VARCHAR(30) NOT NULL, archived_at TIMESTAMPTZ DEFAULT now())` table inside `039.upgrade()` **before** the drop:
```sql
CREATE TABLE files_state_archive (file_id uuid PRIMARY KEY, state varchar(30) NOT NULL, archived_at timestamptz NOT NULL DEFAULT now());
INSERT INTO files_state_archive (file_id, state) SELECT id, state FROM files;
```
This is a forensic/rollback safety net: it preserves the exact scalar the derived `downgrade()` can only *approximate*. **Design tension to resolve (open question):** MIG-04 + D-03 say `downgrade()` reconstructs from *derived* sources; the ROADMAP success criterion adds "archive." These are complementary, not conflicting — recommend: `downgrade()` uses derived reconstruction (D-03) as its documented, milestone-required behavior, and the archive table is an independent operator artifact the runbook can consult (or `downgrade()` MAY prefer it when present for an *exact* restore, falling back to derived for rows created after archival). The planner/discuss-phase should pick one and note it. `[ASSUMED — ROADMAP adds archival beyond CONTEXT's D-list; needs a decision.]`

**"Delta backfill for anything changed since 032" concretely:** migration `032` did the additive backfill (created markers/sidecars *from* `files.state`). Between the `032` deploy and the `039` run, PR-B removes the dual-writers, so from that point the derived sources are the sole authority. The "delta" is any row whose `files.state` implies a marker that isn't present. **The shadow-compare green precondition (D-06 guard) subsumes this:** if `hard_fail_total == 0`, by construction no such delta exists. Recommendation: implement the guard first (Pattern 3); the "delta backfill" is then an **idempotent top-up that finds nothing on a healthy corpus** — include it as an explicit `INSERT … ON CONFLICT DO NOTHING` top-up for the three backfilled markers (dedup, `analysis.failed_at`, `cloud_job` awaiting) only if the planner wants belt-and-suspenders; otherwise the guard alone satisfies the criterion. `[ASSUMED — reconciliation of ROADMAP wording with D-06; recommend guard-subsumes-delta.]`

### Pattern 3: `039` upgrade self-guard — inline sync SQL (D-06/D-07)

Transcribe the shadow-compare **hard** invariants as anti-join `COUNT`s (never import `shadow_compare.py`). Mirror `038`'s raise-to-rollback structure. Gate ONLY when data exists (avoid the `038`/CR-02 fresh-DB abort):
```python
# Mid-flight guard (D-06): abort only if genuinely mid-flight rows exist.
midflight = bind.execute(sa.text(
    "SELECT (SELECT count(*) FROM files WHERE state IN ('pushing','uploading')) "
    "     + (SELECT count(*) FROM cloud_job WHERE status IN ('uploading','submitted','running'))"
)).scalar_one()
if midflight:
    raise RuntimeError(f"{midflight} mid-flight cloud rows present; drain (--profile drain) before running 039.")

# Shadow-compare implication guard (D-07): one anti-join per HARD invariant, summed.
# state = X AND NOT <derived-condition>  -> must be 0. Example rows (transcribe all 14 hard invariants):
hard_violations = bind.execute(sa.text(
    "SELECT count(*) FROM files f WHERE ("
    " (f.state='analyzed'  AND NOT EXISTS (SELECT 1 FROM analysis a WHERE a.file_id=f.id AND a.analysis_completed_at IS NOT NULL))"
    " OR (f.state='metadata_extracted' AND NOT EXISTS (SELECT 1 FROM metadata m WHERE m.file_id=f.id AND m.failed_at IS NULL))"
    " OR (f.state='analysis_failed' AND NOT EXISTS (SELECT 1 FROM analysis a WHERE a.file_id=f.id AND a.failed_at IS NOT NULL))"
    " OR (f.state='duplicate_resolved' AND NOT EXISTS (SELECT 1 FROM dedup_resolution d WHERE d.file_id=f.id))"
    " OR (f.state='awaiting_cloud' AND NOT EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id=f.id AND c.status='awaiting'))"
    " OR (f.state IN ('pushing','pushed') AND NOT EXISTS (SELECT 1 FROM cloud_job c WHERE c.file_id=f.id))"
    " OR (f.state='proposal_generated' AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id=f.id))"
    " OR (f.state IN ('approved','rejected','executed','failed','moved','unchanged')"
    "     AND NOT EXISTS (SELECT 1 FROM proposals p WHERE p.file_id=f.id AND p.status = "
    "         CASE f.state WHEN 'moved' THEN 'executed' WHEN 'unchanged' THEN 'failed' ELSE f.state END))"
    ")"
)).scalar_one()
if hard_violations:
    raise RuntimeError(f"{hard_violations} shadow-compare implication violations; run the shadow-compare check and reconcile before 039.")
```
- **`fingerprinted` and `local_analyzing` are NOT in the guard** (soft allowlist — they legitimately diverge).
- Verify exact table names at plan time: `analysis` (not `analysis_results` — memory), `metadata`, `dedup_resolution`, `cloud_job`, `proposals`, `execution_log`. `moved↔executed` / `unchanged↔failed` mapping from `shadow_compare.INVARIANTS` (verified).
- Empty-DB safety is automatic: every `COUNT` is 0 on a fresh DB, so nothing raises (D-06 satisfied without a special-case).

### Pattern 4: `downgrade()` reconstruction (D-03/D-04/D-05, MIG-04 lossiness doc)

Recreate the column + index, then one `UPDATE … SET state = CASE …` over ~11,428 rows. **No batching needed** — `038` documents (verified) that a single indexed UPDATE at 11,428 rows takes a sub-second ROW EXCLUSIVE lock; the discretion in D-03 can land on "single statement." The CASE encodes **markers-override-then-furthest-along** (evaluate marker overrides first, else walk the ladder top-down):
```sql
-- Recreate first: op.add_column('files', sa.Column('state', sa.String(30), nullable=False, server_default='discovered'))
-- then op.create_index('ix_files_state','files',['state']); backfill; then op.alter_column drop server_default.
UPDATE files f SET state = CASE
  -- durable marker overrides (D-05), highest priority
  WHEN EXISTS (SELECT 1 FROM dedup_resolution d WHERE d.file_id=f.id) THEN 'duplicate_resolved'
  WHEN EXISTS (SELECT 1 FROM analysis a WHERE a.file_id=f.id AND a.failed_at IS NOT NULL) THEN 'analysis_failed'
  WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id=f.id AND p.status='rejected') THEN 'rejected'
  -- furthest-along ladder (D-04), most-advanced first
  WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id=f.id AND p.status='executed') THEN 'executed'
  WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id=f.id AND p.status='approved') THEN 'approved'
  WHEN EXISTS (SELECT 1 FROM proposals p WHERE p.file_id=f.id) THEN 'proposal_generated'
  WHEN EXISTS (SELECT 1 FROM analysis a WHERE a.file_id=f.id AND a.analysis_completed_at IS NOT NULL) THEN 'analyzed'
  WHEN EXISTS (SELECT 1 FROM fingerprint_results r WHERE r.file_id=f.id AND r.status IN ('success','completed')) THEN 'fingerprinted'
  WHEN EXISTS (SELECT 1 FROM metadata m WHERE m.file_id=f.id AND m.failed_at IS NULL) THEN 'metadata_extracted'
  ELSE 'discovered' END;
```
**Lossy cases to enumerate in the docstring (D-05):** `LOCAL_ANALYZING`, `PUSHING`, `PUSHED`, `AWAITING_CLOUD` (transient dispatch/routing — collapse to the nearest durable stage, typically `analyzed`/`metadata_extracted` or `discovered`); rollback-`FINGERPRINTED` (a file rolled back out of `ANALYSIS_FAILED` with no fingerprint-success row reconstructs as its true derived stage, not `fingerprinted`); `MOVED`/`UNCHANGED` reconstruct as `executed`/`failed` (their proposal-status twins), not the original scalar. **Round-trip test asserts only the durable set:** `metadata_extracted, analyzed, analysis_failed, proposal_generated, approved, rejected, executed, duplicate_resolved` (D-05). If the `files_state_archive` exists, the round-trip test may additionally assert exact restore from it.

### Pattern 5: Migration rehearsal against a real-corpus restore (success-criterion 3)

Two layers:
1. **Committed integration test** (`tests/integration/test_migrations/test_migration_039_*.py`) — mirror `test_migration_038`'s structure: `_reset_schema` → `upgrade_to("038")` → seed representative rows for every durable state + the mid-flight/soft cases → `upgrade_to("039")` → assert column/index gone + archive populated + guard behavior (seed a violation, assert `RuntimeError`; seed empty, assert clean pass) → `downgrade_to("038")` → assert column/index recreated + durable states restored → teardown via `_reset_schema`. **Export `MIGRATIONS_TEST_DATABASE_URL` at `:5433`.** Because `039`'s downgrade IS implemented (unlike `038`), a reversibility mirror is possible; still tear down with `_reset_schema` for isolation.
2. **Operator rehearsal against a restore of the live corpus** (runbook step, satisfies "rehearsal against a restore of the real corpus passes") — use the read-only prod probe recipe (memory `reference_lux_readonly_pg_probe`: `ssh datum@lux.lan`, direct `:5432`, DB `phaze`) to `pg_dump`/restore a copy into a scratch DB, run `alembic upgrade 039` against it under the drain window, confirm the guard passes and row counts reconcile, then `alembic downgrade 038` and confirm durable-state restoration. Record the measured lock-acquisition/DDL timing in the VERIFICATION doc. `[CITED: memory reference_lux_readonly_pg_probe]`

### Anti-Patterns to Avoid
- **Importing `shadow_compare.py` (or any `phaze.services.*`) from the migration** — violates D-07 and the frozen-in-time rule; use raw `sa.text` like `038`.
- **f-stringing any value into migration SQL** — bandit B608 / `S` rules; parameterize with `.bindparams()` (the `lock_timeout` literal is a fixed constant, acceptable).
- **Referencing `saq_jobs` or the scheduling ledger in the migration** — the in-flight signal is app-layer only; the migration's guard uses only durable tables.
- **Batching the downgrade UPDATE** — unnecessary at 11K rows and adds lock churn (038 precedent).
- **Trusting CONTEXT's "~9 writers / no reader consumes state"** — see Pitfall 1.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-stage derived predicate | New EXISTS SQL in each reader | `stage_status.py` `done_clause`/`failed_clause`/`eligible_clause`/`dedup_resolved_clause`/`awaiting_candidate_clause` | Drift-locked to the Python resolver by the equivalence test; re-spelling breaks `test_stage_status_equivalence.py`. |
| Migration guard | New shadow-compare logic | Transcribe `shadow_compare.INVARIANTS` as inline anti-join SQL | The invariants are already the vetted, live-green gate; duplication is the intentional D-07 decoupling cost. |
| Migration scaffolding | Fresh migration idioms | Copy `038` (raw `sa.text`, `bindparams`, `-x` override, raise-to-rollback, empty-autogenerate test) | 038 is the just-shipped, reviewed precedent for exactly this class. |
| Anti-drift guard | Full schema-absence behavioral suite | mypy/ruff (primary) + ONE mutation-tested source-grep test | D-08; a full suite is redundant surface for a one-way migration. |

**Key insight:** Nearly all of Phase 90 is *deletion* plus *pointing existing readers at existing derived builders*. The only genuinely new code is the `039` migration body (DDL wrapper + archive + guard + downgrade CASE) and the one grep guard test.

## Runtime State Inventory

This is a schema-destructive + code-deletion phase. The state audit:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| **Stored data** | `files.state` column (~11,428 prod rows, plain `String(30)`) + `ix_files_state` btree. Prod is at Alembic **031** per memory `project_prod_alembic_031_unreleased` — **032–038 are unreleased to prod.** This means at deploy time the prod DB gains 032→039 in one rollout; the additive markers 032 backfills must land and shadow-compare must be run green **before** 039 executes. | `039` drops column+index; **critical gate:** confirm 032–038 applied + shadow-compare green on the live corpus (drained) before 039. Archive table `files_state_archive` created by 039. |
| **Live service config** | None. No external service (n8n/Datadog/Tailscale/K8s manifest) stores the literal `FileState` names as config. The `cloud_job`/`dedup_resolution`/`analysis` derived sources are already the live authority (readers `get_awaiting_cloud_count`, `get_cloud_staging_candidates`, dedup readers already cut over). | None — verified by grep: only Python/SQL/HTML in-repo reference `FileState`. |
| **OS-registered state** | None. No Task Scheduler / pm2 / systemd unit embeds `FileState`. | None — verified. |
| **Secrets/env vars** | None reference `FileState` or `files.state`. | None — verified. |
| **Build artifacts / installed packages** | Deleting the `FileState` class changes `phaze.models.file` and `phaze.models.__init__` (re-exports `FileState` — verify import list). No compiled artifact caches the enum. `uv run` resolves from source. | Update `models/__init__.py` re-export; `uv sync` not required (no dep change). |

**Deploy-ordering (the load-bearing runtime fact):** prod@031 + unreleased 032–038 means the destructive 039 must not be the *first* thing prod sees. The runbook order is: deploy code through 038 (additive markers backfilled) → run shadow-compare against the live, drained corpus → confirm `hard_fail_total==0` → **then** run 039. `[CITED: memory project_prod_alembic_031_unreleased]`

## Common Pitfalls

### Pitfall 1 (HIGHEST SEVERITY): CONTEXT's inventory is incomplete AND its PR-ordering premise is falsified

**What goes wrong:** CONTEXT's `SCOPE DISCOVERY` lists ~9 writers and a handful of readers and asserts (D-01) "PR-1 pure writer removal … no reader consumes state." Both are wrong:

- **Writers are ~17, not ~9.** The note enumerated only the `file.state = …` *attribute-assignment* form and missed every `update(FileRecord).values(state=…)` form and the INSERT-time stamp. **Complete verified writer inventory (all in `src/phaze/`):**
  1. `routers/agent_files.py:111` — `data["state"]=DISCOVERED` (bulk-upsert INSERT stamp) ⚠️ not in CONTEXT
  2. `routers/agent_metadata.py:106` — `update(...).where(state==DISCOVERED).values(state=METADATA_EXTRACTED)` (CAS; **embeds a state READ**) ⚠️ not in CONTEXT
  3. `routers/agent_analysis.py:247` — `.values(state=ANALYZED)` ⚠️ not in CONTEXT
  4. `routers/agent_analysis.py:382` — `.values(state=ANALYSIS_FAILED)` (the D-05 "dies in 90" write; comment at :380)
  5. `routers/pipeline.py:999` — `file.state=DISCOVERED`
  6. `routers/pipeline.py:1129` — `f.state=FINGERPRINTED`
  7. `routers/pipeline.py:1273` — `file.state=FINGERPRINTED`
  8. `routers/agent_push.py:151` — `.values(state=PUSHED)` ⚠️ not in CONTEXT
  9. `routers/agent_push.py:306` — `.values(state=AWAITING_CLOUD)` (spill) ⚠️ not in CONTEXT
  10. `routers/agent_s3.py:128` — `update(...).where(state==PUSHING).values(state=PUSHED)` (CAS; **embeds a state READ**) ⚠️ not in CONTEXT
  11. `routers/agent_s3.py:232` — `.values(state=AWAITING_CLOUD)` (spill) ⚠️ not in CONTEXT
  12. `services/backends.py:124` — `AWAITING_CLOUD`
  13. `services/backends.py:304` — `LOCAL_ANALYZING`
  14. `services/backends.py:395` — `PUSHING` (compute)
  15. `services/backends.py:508` — `PUSHING` (kueue)
  16. `services/dedup.py:274` — `f.state=DUPLICATE_RESOLVED`
  17. `services/dedup.py:346` — `update(...).values(state=restore_by_id[...])` (dedup UNDO restore) ⚠️ not in CONTEXT

- **~11 live `files.state` READERS remain, several coupled to PR-1 writers.** Complete verified reader inventory (see Architecture map for derived targets):
  - `services/pipeline.py:981` `get_files_by_state` (generic; only live caller `get_analysis_failed_files`; tests also call it)
  - `services/pipeline.py:1031/1042/1070` `get_analyze_stage_files` (`_ANALYZE_STAGE_STATES` + `completed=state==ANALYZED` + template `f.state` reads)
  - `services/pipeline.py:1306` `get_analysis_failed_count`
  - `services/pipeline.py:1474` `get_pushing_count`
  - `services/pipeline.py:1489` `get_pushed_count`
  - `services/pipeline.py:1569` `_backfill_candidates_stmt` (feeds `count_backfill_candidates` + the analyze re-drive backfill) ⚠️ not in CONTEXT
  - `services/pipeline.py:1707` `get_proposal_pending_batches` (`state.in_([ANALYZED, METADATA_EXTRACTED])`)
  - `routers/pipeline.py:1040` `held_files = [… if file.state == AWAITING_CLOUD]` ⚠️ not in CONTEXT
  - `routers/pipeline.py:1247` `retry_analysis_failed` reader (`state==ANALYSIS_FAILED`) ⚠️ not in CONTEXT
  - `services/search_queries.py:66,88` — **entire SEARCH facet**: `SELECT FileRecord.state.label('state')` + `WHERE state==file_state` ⚠️ **completely absent from CONTEXT**
  - `services/dedup.py:270` — `previous_state: f.state` capture for the dedup UNDO (restored at `:346`; the `duplicates.py` router threads it through `file_states` form JSON) ⚠️ not in CONTEXT
  - Plus the two **CAS-guard reads embedded in writers** #2 (`agent_metadata:106`) and #10 (`agent_s3:128`).

- **The coupling that breaks D-01/D-02's premise:** removing writer #12 (`backends.py:124`, `AWAITING_CLOUD`) in a "pure writer" PR-1 leaves the column present but stale, so the live reader `routers/pipeline.py:1040` (`held_files … state==AWAITING_CLOUD`) stops seeing newly-held files → the ledger-seed for held files silently breaks. The same coupling holds for PUSHING/PUSHED writers ↔ `get_pushing_count`/`get_pushed_count`/`get_analyze_stage_files`; ANALYSIS_FAILED writer ↔ `get_analysis_failed_*`/`_backfill_candidates`/`retry` reader; METADATA_EXTRACTED+ANALYZED writers ↔ `get_proposal_pending_batches`; DUPLICATE_RESOLVED writer ↔ the dedup-undo `previous_state` capture.

**How to avoid — RECOMMENDATION (escalate to planner/discuss-phase):** invert the order to **readers-first**: **PR-A** convert every live reader to derived sources (column intact, fully reversible) → **PR-B** remove all writers (now truly unconsumed) → **PR-C** destructive `039`. This preserves D-01's three-independently-green-PRs discipline and D-02's "all readers before the drop," and fixes the falsified "writers-first is behavior-preserving" premise. If the user insists on writers-first, then only the writers whose readers are already dead may go in the first PR (candidates: `FINGERPRINTED` writes #6/#7 and possibly `DISCOVERED` writes #1/#5 — *verify no live reader* first), and every writer coupled to a live reader must move to the same PR as its reader's cutover. **This is a locked-decision-premise conflict and should be surfaced, not silently resolved.**

**Warning signs:** an integration test that asserts a dashboard count or the analyze-workspace table stays correct after PR-1 will go RED; the `held_files` ledger-seed path is the subtlest (no test may cover it — mirror memory `project_htmx_hxon_alpine_scope_trap`: passing tests ≠ correct).

### Pitfall 2: The SEARCH `file_state` facet (`search_queries.py`) has no derived home

**What goes wrong:** `search_files_and_tracklists(... file_state=...)` both SELECTs `FileRecord.state` (a result column) and filters `WHERE state==file_state`. There is no single derived scalar to replace a free-text state facet — the derived model is per-stage multi-valued.
**How to avoid:** decide at plan time — either (a) drop the `file_state` facet + its `state` result column (simplest; a single-user admin tool), or (b) map the facet to a derived bucket via `stage_status_case`. Recommend (a) unless the facet is in active use. Requires touching the search route + template. `[ASSUMED — needs a product decision.]`

### Pitfall 3: PUSHING vs PUSHED cannot be distinguished from `cloud_job.status` post-drop

**What goes wrong:** `get_pushing_count` (`state==PUSHING`) and `get_pushed_count` (`state==PUSHED`) drive two separate dashboard cards ("Staged (pushing)" / "Analyzing (cloud)"). Shadow-compare **loosened** both to mere `cloud_job`-row existence (`_cloud_job_exists`) precisely because a live cloud file legitimately advances `uploading→uploaded→submitted→running` and no single `cloud_job.status` cleanly means "pushing" vs "pushed" across compute/kueue/s3 backends (verified `CloudJobStatus`: `uploading, uploaded, submitted, running, succeeded, failed, awaiting`).
**How to avoid:** the planner must pin a status→card mapping. Reasonable default: `pushing = cloud_job.status IN ('uploading','submitted')`, `pushed = cloud_job.status IN ('uploaded','running')`. Alternative: collapse the two cards into one "cloud in-flight" count. Because deploy happens under `--profile drain`, both counts are ~0 at migration time, but the readers run on live traffic after drain lifts. `[ASSUMED — status mapping is a UI-fidelity decision.]`

### Pitfall 4: `get_proposal_pending_batches` state filter also excludes already-proposed files

**What goes wrong:** its `state.in_([ANALYZED, METADATA_EXTRACTED])` did double duty — (a) require metadata+analyze done (redundant with its two existing `EXISTS` clauses) and (b) **exclude files past the propose stage** (state had advanced to `PROPOSAL_GENERATED`/`APPROVED`/…). A naive cutover that just deletes the state filter would re-propose files that already have proposals.
**How to avoid:** replace the state filter with `~exists(proposals WHERE file_id)` (i.e. `~done_clause(Stage.PROPOSE)`) in addition to the existing metadata + completed-analysis EXISTS — equivalently `eligible(PROPOSE)`. Verified: the two EXISTS clauses already cover done(metadata)∧done(analyze); only the not-yet-proposed exclusion must be re-added.

### Pitfall 5: The `:5433` migrations-DB export footgun

**What goes wrong:** `just test-bucket integration` does not export `MIGRATIONS_TEST_DATABASE_URL`; the harness silently falls back to `:5432` and the migration test "fails like an infra flake" (documented in `test_migration_038`'s header, and memory `reference_migrations_test_db_port`).
**How to avoid:** export `MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"` before running the 039 test; provision via `just test-db` (5433) + `just test-bucket`.

## Code Examples

Derived-target signatures (verified in `services/stage_status.py`) — the exact builders PR-A readers compose:
```python
# analyze-failed reader cutover (get_analysis_failed_count / _backfill_candidates_stmt / retry reader):
#   OLD: .where(FileRecord.state == FileState.ANALYSIS_FAILED)
#   NEW: .where(failed_clause(Stage.ANALYZE))     # exists(analysis WHERE file_id AND failed_at IS NOT NULL)

# proposal convergence reader cutover (get_proposal_pending_batches):
#   OLD: .where(FileRecord.state.in_([FileState.ANALYZED, FileState.METADATA_EXTRACTED]))
#   NEW: keep the two EXISTS(metadata)/EXISTS(analysis completed) clauses,
#        replace the state filter with  ~done_clause(Stage.PROPOSE)   # ~exists(proposals)

# awaiting-cloud in-memory sub-filter (routers/pipeline.py:1040 held_files):
#   get_cloud_staging_candidates already scopes to awaiting_candidate_clause(); the in-memory
#   `state == AWAITING_CLOUD` filter is redundant -> drop it (all returned candidates are awaiting).
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Linear `FileRecord.state` scalar as pipeline authority | Derived per-stage status over output/marker tables | Phases 77–89 | Phase 90 removes the last vestige (column + enum + writers). |
| Irreversible migration → `NotImplementedError` downgrade (Phase 89/038 D-10) | Best-effort derived-reconstruction downgrade (D-03) | Phase 90 | MIG-04 explicitly requires reconstruct-from-derived + documented lossiness. |

**Deprecated/outdated:**
- Design §7 "Call-site inventory" reader list — **stale** (CONTEXT flags this; this research supersedes it with the verified inventory in Pitfall 1).
- `shadow_compare.py` docstring references "the destructive `033` migration" — stale label; the actual revision is **`039`** (cosmetic; do not couple to it).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Readers-first PR ordering is safer than the locked writers-first (D-01) | Pitfall 1 | If overridden without care, PR-1 ships a silently-broken `held_files` ledger-seed / dashboard counts on the live corpus. |
| A2 | `downgrade()` uses derived reconstruction (D-03) as primary; `files_state_archive` is a separate operator artifact | Pattern 2 | Mis-scopes the archive→downgrade relationship; ROADMAP adds "archive" beyond CONTEXT's D-list — needs a decision. |
| A3 | Shadow-compare green precondition subsumes the "delta backfill since 032" (guard finds it → no delta) | Pattern 2 | If a genuine post-032 delta exists that the guard doesn't cover, the top-up is still needed. |
| A4 | Drop the SEARCH `file_state` facet rather than derive it | Pitfall 2 | Removes a facet that may be in use; alternative is a `stage_status_case` mapping. |
| A5 | `pushing = status IN (uploading,submitted)`, `pushed = status IN (uploaded,running)` | Pitfall 3 | Dashboard card fidelity; drained at migration time so low blast radius. |
| A6 | `env.py` savepoint-retry works under the repo's alembic transaction mode | Pattern 1 | If the outer txn is left aborted between attempts, retry fails — verify `env.py` at plan time. |
| A7 | `FINGERPRINTED` / `DISCOVERED` writes have no live reader (safe for a writers-first first-PR subset) | Pitfall 1 | Must be grep-verified before relying on it; a missed reader breaks. |

## Open Questions (RESOLVED)

*All four resolved during plan-phase (2026-07-12) via AskUserQuestion → recorded in CONTEXT.md D-09..D-12.*

1. **PR ordering (A1).** RESOLVED: see **D-09** — readers-first (PR-A readers → PR-B writers → PR-C destructive).
2. **Archive vs derived downgrade (A2).** RESOLVED: see **D-10** — lossless archive-restore is the primary `downgrade()` path; the derived reconstruction (D-04/D-05) is the fallback for post-039 rows only.
3. **SEARCH facet (A4).** RESOLVED: see **D-11** — drop the `file_state` search facet (filter + result column + route/template surfaces).
4. **PUSHING/PUSHED card mapping (A5).** RESOLVED: see **D-12** — two cards, `pushing = cloud_job.status IN ('uploading','submitted')`, `pushed = cloud_job.status IN ('uploaded','running')`.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (migrations test DB, `:5433`) | 039 integration test | via `just test-db` | 16+ | — (blocking for the migration test) |
| `MIGRATIONS_TEST_DATABASE_URL` env export | 039 integration test | must be set manually | — | none — unset silently hits `:5432` |
| Read-only prod probe (`ssh datum@lux.lan`, DB `phaze`) | rehearsal against real-corpus restore | operator-run | — | scratch restore of a `pg_dump` |
| uv / ruff / mypy / pytest | all work | ✓ | project-pinned | — |

**Missing with no fallback:** none blocking code work; the operator rehearsal (Pattern 5 layer 2) is deployment-gated and runs against a live-corpus restore during the drain window.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (via `uv run pytest`) |
| Config | `pyproject.toml` + `tests/buckets.json` (per-bucket isolation; `tests/shared/test_partition_guard.py` enforces one bucket per file) |
| Quick run | `uv run pytest tests/shared/services/test_pipeline.py -x` (reader cutovers) |
| Migration run | `MIGRATIONS_TEST_DATABASE_URL=…:5433/phaze_migrations_test just test-bucket integration` |
| Full suite | `uv run pytest` (90% floor; re-run failed subset in isolation on colima flake — memory `reference_local_fullsuite_colima_flake`) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Command | Exists? |
|-----|----------|-----------|---------|---------|
| MIG-04 | 039 drops column+index, deletes enum, guard aborts on violation/mid-flight, clean on empty | integration (migration) | `just test-bucket integration` (with `:5433` export) | ❌ Wave 0 — `test_migration_039_*.py` |
| MIG-04 | `downgrade()` restores column+index + backfills durable states | integration | same | ❌ Wave 0 |
| MIG-04 | anti-drift: `FileState`/`files.state`/`.state =` cannot reappear in `src/` | unit (source-grep) | `uv run pytest tests/shared/…/test_no_filestate_guard.py` | ❌ Wave 0 (**mutation-test it** — memory `feedback_mutation_test_guard_tests`) |
| D-01 readers | each converted reader returns correct rows from derived sources (counts, analyze workspace, proposal batches, backfill, search) | unit/integration | `just test-bucket analyze` / `metadata` / `shared` | Partial — extend existing `test_pipeline.py` |
| MIG-04 | no reader/writer of `state` survives; equivalence test still green | integration | `test_stage_status_equivalence.py` | ✅ exists |

### Wave 0 Gaps
- [ ] `tests/integration/test_migrations/test_migration_039_*.py` — upgrade (guard: violation→raise, empty→pass, mid-flight→raise), archive populated, DDL gone; downgrade restores durable states. Model on `test_migration_038`.
- [ ] `tests/shared/.../test_no_filestate_guard.py` — mutation-tested source-grep (D-08).
- [ ] Extend `tests/shared/services/test_pipeline.py` + analyze/metadata bucket tests for each reader cutover; **add coverage for the `held_files` ledger-seed path** (currently likely uncovered — Pitfall 1 warning sign).
- [ ] Delete/repoint `get_files_by_state` tests once that helper is removed.

## Security Domain

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | yes | Migration SQL parameterized via `sa.text().bindparams()` — never f-string (bandit B608 / `S` rules); the only literal is the fixed `lock_timeout` constant. |
| V6 Cryptography | no | — |
| V2/V3/V4 | no | No auth/session/access-control surface changes. |

| Threat Pattern | STRIDE | Mitigation |
|----------------|--------|------------|
| SQL injection via migration/guard SQL | Tampering | Parameterized `bindparams`; no interpolated operands (038 discipline). |
| Destructive drop on a mid-flight/unhealthy corpus | Tampering/DoS | D-06 self-guard (mid-flight + shadow-compare implication) aborts the txn before the drop; `--profile drain` quiesce; `files_state_archive` forensic snapshot. |
| PII leakage in migration/test output | Info disclosure | Guard/rehearsal emit counts + `file_id` UUIDs only, never `original_path`/`original_filename` (shadow-compare T-79-02 precedent). |

## Sources

### Primary (HIGH confidence)
- Codebase (verified this session): `models/file.py`, `services/pipeline.py`, `services/stage_status.py`, `enums/stage.py`, `services/shadow_compare.py`, `services/backends.py`, `services/search_queries.py`, `routers/{pipeline,agent_push,agent_s3,agent_metadata,agent_analysis,agent_files}.py`, `services/dedup.py`, `alembic/versions/038_retire_legacy_sentinel.py`, `tests/integration/test_migrations/{conftest.py,test_migration_038_*}`, `.planning/{ROADMAP,REQUIREMENTS,STATE}.md`, `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md`, `90-CONTEXT.md`.
- Project memory (auto-loaded): `project_prod_alembic_031_unreleased`, `reference_lux_readonly_pg_probe`, `reference_migrations_test_db_port`, `feedback_mutation_test_guard_tests`, `analyzed` invariant / table-name note.

### Secondary (MEDIUM confidence)
- Postgres `DROP COLUMN` lock/catalog-only + `lock_timeout` retry semantics: dev.to "Which ALTER TABLE Operations Lock Your PostgreSQL Table", leopard.in.ua "Safe and unsafe operations for high volume PostgreSQL".

Sources:
- [Which ALTER TABLE Operations Lock Your PostgreSQL Table?](https://dev.to/mickelsamuel/which-alter-table-operations-lock-your-postgresql-table-1082)
- [Safe and unsafe operations for high volume PostgreSQL](http://leopard.in.ua/2016/09/20/safe-and-unsafe-operations-postgresql)

## Metadata

**Confidence breakdown:**
- Reader/writer inventory: HIGH — every site grep-verified with line numbers and read in context.
- `039` DDL + guard + downgrade design: HIGH — templated on shipped `038`; Postgres lock semantics doc-confirmed.
- PR-ordering finding: HIGH — the read-coupling is demonstrable from the code; the *resolution* (readers-first) is a MEDIUM recommendation needing user sign-off (locked-decision conflict).
- Archival / delta-backfill reconciliation: MEDIUM — ROADMAP adds terms beyond CONTEXT's D-list; flagged as open questions.

**Research date:** 2026-07-12
**Valid until:** 2026-08-11 (stable in-repo domain; re-verify if Phases 82–89 land further reader/writer edits before planning).
</content>
</invoke>
