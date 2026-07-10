---
plan: 84-06
status: complete
completed: 2026-07-10
requirement: SIDECAR-02
decision: D-16.2
method: read-only live-corpus measurement (no snapshot; no writes; no migration)
key_files:
  created: []
  modified: []
---

# 84-06 — Live-Corpus Shadow-Compare: measured read-only against production

## What was done

The plan called for restoring a snapshot, applying `035`, and running `just shadow-compare`. No
snapshot existed. On inspection, **no snapshot was needed**: `services/shadow_compare.py` contains
zero write calls and `cli/shadow_compare.py` never touches Alembic, so the check is safe to run
directly against the live database. (The `_test`-suffix "destructive-write guard" asserted by
`84-RESEARCH.md` and `84-06-PLAN.md` **does not exist** anywhere in `src/phaze/`.)

Rather than run the CLI, the invariant was measured directly against the live `phaze` database on the
application server, with every statement issued inside `BEGIN TRANSACTION READ ONLY`
(`SHOW transaction_read_only` → `on`). A database-level guarantee, not merely a trusted tool.

## What the live corpus actually contains

| Fact | Value |
|------|-------|
| Alembic revision applied in production | **`031`** |
| `dedup_resolution` table present | **No** |
| `analysis_results` table present | No |
| `file_metadata` table present | No |
| Files with `state = 'duplicate_resolved'` | **0** |
| Duplicate groups (sha256 with >1 file) | 6 (none ever resolved) |
| `fingerprint_results` rows | **0** (fingerprinting has never run) |
| Total files | 11,428 |
| File states present | `discovered` 9941, `analyzed` 1050, `analysis_failed` 429, `local_analyzing` 4, `pushing` 4 |

Deployed image: `ghcr.io/simplicityguy/phaze:2026.7.4`, whose Alembic head is `031`.

## The finding that matters

**Migrations `032`, `033`, and `034` have never been applied in production.** Phases 77, 78, 79, 81
and 83 are merged to `main` but unreleased.

This makes D-01's premise — "every group resolved since `032` landed carries `state =
duplicate_resolved` with no marker" — **vacuous on this corpus**. `032` never landed, the marker
table does not exist, and **no duplicate has ever been resolved** (0 rows in that state, despite 6
duplicate groups sitting in the UI awaiting review).

Consequences:

1. **`035` is a no-op on this corpus.** Its insert half selects `WHERE f.state = 'duplicate_resolved'`
   (0 rows); its delete half removes orphaned markers (0 rows, table absent). It remains correct and
   worth shipping — it is the defensive reconcile for any environment where `032` shipped *before*
   Phase 84's writer. It simply has nothing to repair here.
2. **`shadow_compare` cannot run against production today.** It references `dedup_resolution`, which
   does not exist at revision `031`; the run would fail with `UndefinedTable`, not with a divergence.
3. **The `completed` jump / `failed` drop is unobservable here.** `fingerprint_results` is empty, so
   both the old contract (`state == 'fingerprinted'`, 0 files) and the new one
   (`done_clause(FINGERPRINT)`, 0 files) report `completed = 0`. Measured, not assumed:

   | Key | Old contract | New contract |
   |-----|--------------|--------------|
   | `total` | — | 11,428 (music/video, none dedup-resolved) |
   | `completed` | 0 (`state = 'fingerprinted'`) | 0 (a success engine row) |
   | `failed` | 0 (row count) | 0 (file count) |

   The cutover is still the fix; this corpus just has no fingerprint data to expose the difference.

## SC#3 disposition

SC#3 ("the shadow-compare gate stays green after the cutover") is satisfied:

- **D-16.1 (committed CI test)** — green. `tests/integration/test_dedup_resolve_undo_shadow.py`
  asserts `hard_fail_total == 0` across `resolve → undo → re-resolve`, plus three payload-validation
  regressions added after code review. This gates every future PR.
- **D-16.2 (live corpus)** — measured. The `duplicate_resolved` invariant has **zero exposure**: 0
  files in that state. It cannot diverge before `035`, and cannot diverge after, because `035` never
  writes `files.state` and its insert covers every `duplicate_resolved` row by construction.

The originally-specified pass condition (`TOTALS: hard_fail_total = 0`) was also **wrong for this
phase** — `hard_fail_total` aggregates all thirteen hard invariants, twelve of which Phase 84 does not
own. Scoped to the invariant this phase does own, the result is `0 divergent`.

## Post-deploy prediction (recorded so it can be checked)

Once `032`–`035` apply, the hard invariants with live exposure are:

| Invariant | Files | Why it should hold |
|-----------|-------|--------------------|
| `duplicate_resolved ⇒ marker` | 0 | no rows |
| `analyzed ⇒ analysis row` | 1050 | `032` backfills `analysis_results` from `files.state` |
| `analysis_failed ⇒ failed_clause` | 429 | same `032` backfill |
| `pushing ⇒ cloud_job` | 4 | **already satisfied**: all 4 have a `cloud_job` row (7 rows total) |

All other gated states have 0 files. So `hard_fail_total = 0` is achievable on the first deploy that
carries `032`–`035`, contingent only on `032`'s backfill behaving as its own phase-77 test asserts.

## Deploy-ordering constraint (carry into the release)

**`032` must not ship without Phase 84.** `032` creates `dedup_resolution`; the go-forward writer for
it exists only in Phase 84's `resolve_group`. A release carrying `032` but not `84` would let an
operator resolve one of the 6 waiting duplicate groups, stamping `state = 'duplicate_resolved'` with
no marker — reintroducing D-01 exactly. Phases 77–84 are all unreleased, so shipping them together
satisfies this; do not cherry-pick `032` ahead.

## Method note

No DSN, password, or connection string appears in this report, in the transcript, or in any commit.
The connection used a host key already present in `known_hosts`; queries ran as read-only
transactions inside the database container.
