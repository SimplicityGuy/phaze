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
| `analysis` table present (backs `done_clause(ANALYZE)`) | Yes — 1214 rows, but only **49** have `analysis_completed_at` set |
| `file_metadata` table present | No (the metadata table is named `metadata`) |
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
table does not exist, and there are **0 rows in that state**, despite 6 duplicate groups sitting in
the UI awaiting review.

> **Why 0, corrected 2026-07-10:** this was originally read as "no operator ever resolved a duplicate".
> The Phase-84 UAT found a better explanation — `routers/duplicates.py` never committed its
> transaction, so **every resolve silently rolled back** while the UI reported success. Resolutions
> may well have been clicked. Fixed in `74f1f12f`; see `84-UAT.md` tests 4–5.

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

## Post-deploy prediction — CORRECTED 2026-07-10 (was wrong)

> **The original prediction in this section was incorrect and has been replaced.** It named a table
> (`analysis_results`) that does not exist, and assumed `032` backfills the analyze-completed rows.
> Both were wrong. Found by the Phase-84 UAT (`84-UAT.md`, test 8).

Once `032`–`035` apply, the hard invariants with live exposure are:

| Invariant | Files | Post-deploy status |
|-----------|-------|--------------------|
| `duplicate_resolved ⇒ marker` | 0 | **0 divergent** — no rows, and `035` never writes `files.state` |
| `pushing ⇒ cloud_job` | 4 | **0 divergent** — all 4 already have a `cloud_job` row (7 rows total) |
| `analysis_failed ⇒ analysis.failed_at` | 429 | **0 divergent** — `032`'s `_BACKFILL_ANALYZE_FAILED` upserts it |
| `analyzed ⇒ analysis.analysis_completed_at IS NOT NULL` | 1050 | ⚠ **~1001 divergent** |

**`hard_fail_total = 0` is NOT reachable on the first deploy.** The analyze stage is backed by table
**`analysis`** (`models/analysis.py` → `AnalysisResult`), not `analysis_results`.
`done_clause(Stage.ANALYZE)` requires `analysis.analysis_completed_at IS NOT NULL` (DERIV-03,
`stage_status.py:123`). Measured read-only on production: all 1050 `analyzed` files have an `analysis`
row, but only **49** have `analysis_completed_at` set — 1001 have it NULL (1165 of 1214 total rows are
NULL). `032` backfills `analysis.failed_at` for `analysis_failed` files only
(`_BACKFILL_ANALYZE_FAILED`); **nothing in `032`–`035` populates `analysis_completed_at`.**

Since the `analyzed` invariant is HARD (`soft=False`, `shadow_compare.py` registry), the first
`just shadow-compare` after deploy will report ~1001 divergences and **exit 1**.

**This is not a Phase 84 defect** — Phase 84's own invariant (`duplicate_resolved`) is clean. It is a
milestone-level data gap: `analysis_completed_at` is a newer column that legacy rows never populated,
and Phase 79 deferred the live gate run that would have surfaced it (79 D-02) — the same root cause as
D-01. Do not read a red `hard_fail_total` after deploy as a Phase 84 regression.

**Open item (owner: milestone / Phase 79 follow-up).** Decide between:
1. backfill `analysis.analysis_completed_at` from `updated_at` for `state='analyzed'` rows (a `036`
   data migration, mirroring `032`'s analyze-failed upsert), or
2. move `analyzed` to the soft allowlist with a documented rationale, or
3. accept a non-zero `hard_fail_total` until Phase 90 and gate only on named invariants.

Until that is settled, the deploy check should be scoped to the invariant this phase owns:
`duplicate_resolved: 0 divergent`.

---

## Deploy-ordering constraint (carry into the release)

**`032` must not ship without Phase 84.** `032` creates `dedup_resolution`; the go-forward writer for
it exists only in Phase 84's `resolve_group`. A release carrying `032` but not `84` would let an
operator resolve one of the 6 waiting duplicate groups, stamping `state = 'duplicate_resolved'` with
no marker — reintroducing D-01 exactly. Phases 77–84 are all unreleased, so shipping them together
satisfies this; do not cherry-pick `032` ahead.

**Status: satisfied structurally** (operator, 2026-07-10). Nothing deploys to production except via a
tagged release, and no release will be tagged before Phase 84 merges. `032` and Phase 84 therefore land
in the same image, so the marker table can never exist in production without its writer. The constraint
requires no further action — it is recorded here so a future cherry-pick or hotfix does not violate it.

**Deploy-time expectation for that first release:** it will apply `032`, `033`, `034`, `035` in one
`alembic upgrade head`. `032` upserts `analysis.failed_at` for the 429 `analysis_failed` files and
gap-fills `cloud_job`; `035` is a no-op (0 rows both halves). Re-run `just shadow-compare` afterwards
and expect the **`duplicate_resolved` invariant line to read `0 divergent`** — but expect
`hard_fail_total` to be **non-zero** (~1001) because of the unrelated `analyzed` invariant, per the
corrected prediction table above. That run is a **deploy checklist item, not a merge blocker**, and a
red `hard_fail_total` is NOT a Phase 84 regression.

## Method note

No DSN, password, or connection string appears in this report, in the transcript, or in any commit.
The connection used a host key already present in `known_hosts`; queries ran as read-only
transactions inside the database container.
