---
plan: 84-06
status: resolved
blocking: false
gate: pre-merge
resolved_by: 84-06-SUMMARY.md
resolved_on: 2026-07-10
deferred_on: 2026-07-10
requirement: SIDECAR-02
decision: D-16.2
---

# 84-06 — Live-Corpus Shadow-Compare: RESOLVED

> **Superseded by `84-06-SUMMARY.md` (2026-07-10).** The run was performed read-only against the live
> database — no snapshot was needed, because `shadow_compare` never writes. The corpus turned out to be
> at Alembic revision `031` with **zero** `duplicate_resolved` files and no `dedup_resolution` table, so
> the invariant this phase owns has zero exposure. See the summary for the measurement and the
> deploy-ordering constraint it surfaced. The text below is retained for provenance.

*(Historical, as written on 2026-07-10 before the read-only run.)* Plan 84-06 was initially deferred:
it is `autonomous: false` and was believed to require a snapshot restore plus an operator-supplied DSN.
That belief rested on the non-existent `_test`-suffix guard — see Corrections below. It has since been
executed read-only against the live database; see `84-06-SUMMARY.md`.

## Why this cannot be skipped

D-16 proves SC#3 two ways:

1. **D-16.1 — committed CI test.** Shipped in 84-03
   (`tests/integration/test_dedup_resolve_undo_shadow.py`): asserts
   `run_shadow_compare(session).hard_fail_total == 0` across `resolve → undo → re-resolve`
   on a *synthetic* corpus. This gates every future PR.

2. **D-16.2 — live-corpus run.** ← **THIS, still open.**

The CI test **provably cannot** cover D-16.2. Its corpus is constructed by the test itself,
so it can never contain the real post-`032` `state='duplicate_resolved'`-without-marker rows
that D-01 discovered. Only a run against a production restore proves migration `035` actually
repaired them.

Phase 79 built this gate to be re-runnable and then **deferred the live run** (79 D-02).
That deferral is precisely why D-01 — a `dedup_resolution` table with no go-forward writer
since migration `032` — went unnoticed across two phases. Deferring it a second time without
closing it before merge would repeat the exact failure.

## Corrections to the original plan (verified in source, 2026-07-10)

Three assertions carried from `84-RESEARCH.md` into `84-06-PLAN.md` are **wrong**. They are
corrected here; do not follow the plan's runbook verbatim.

1. **There is no `_test`-suffix "destructive-write guard."** `grep` finds no such check anywhere in
   `src/phaze/`. `services/shadow_compare.py` contains **zero** write calls (no insert/update/delete/
   commit/flush/add), and `cli/shadow_compare.py` imports neither `main` nor Alembic — with
   `--database-url` it builds a fresh engine, runs `SELECT`s, and disposes it. **A snapshot restore is
   not required.** The tool can be pointed at the live database read-only.

2. **A restore is not required for correctness, because the repair is provable by construction.**
   `035`'s insert is `INSERT … SELECT … FROM files f WHERE f.state = 'duplicate_resolved'
   ON CONFLICT (file_id) DO NOTHING`, and `035` never writes `files.state`. So after it runs, every
   `duplicate_resolved` file has a marker — unconditionally. The orphan `DELETE` only removes markers
   where `state <> 'duplicate_resolved'`, so it cannot break that. The insert cannot fail:
   `canonical_file_id` is `nullable=True` (`models/dedup_resolution.py:53`), so the `ORDER BY c.id
   LIMIT 1` subselect may return NULL harmlessly, and `gen_random_uuid()` is built into PG 13+.

3. **`hard_fail_total = 0` is the wrong pass condition for this phase.** `hard_fail_total` aggregates
   **all thirteen** hard invariants in `services/shadow_compare.py`'s registry (`analyzed ⇒ analysis
   row`, `approved ⇒ proposal`, `pushed ⇒ cloud_job`, …). Phase 79 never ran the gate live, so the
   health of the other twelve on the real corpus is **unknown**. If any is red for reasons unrelated
   to dedup, `hard_fail_total = 0` can never pass no matter what Phase 84 does. The claim Phase 84
   actually owns is the **`duplicate_resolved` invariant line**.

## Runbook (read-only, no snapshot, no migration)

Run from this worktree. The tool prints only `host/db` — the DSN password is never echoed.

```bash
# Connect DIRECTLY to Postgres, not through PgBouncer: the run opens one connection, and the
# homelab has a prior pool-exhaustion incident on the session-mode pool.
just shadow-compare --database-url 'postgresql+asyncpg://<user>:<pw>@<pg-host>:5432/phaze'
```

Expect **exit 1** before `035` has been applied — that is the evidence, not a failure. Record:

- the `duplicate_resolved` line: `N divergent` ← this is D-01, measured on the real corpus
- every other invariant line ← first-ever live read on the other twelve
- the `TOTALS:` line

Then, after `035` applies during the normal deploy (`PHAZE_AUTO_MIGRATE` → `main.py:87`), re-run the
same command and confirm the `duplicate_resolved` line reads `0 divergent`.

## Pass condition

- The `duplicate_resolved` invariant line reads `0 divergent` **after `035` has been applied**.
- Pre-`035`, a non-zero `duplicate_resolved` count is the expected, sought-after result.
- `TOTALS: hard_fail_total` and the other twelve invariants are **recorded, not gated** — they are
  corpus health data this phase does not own. Any non-zero unrelated invariant becomes a follow-up
  item, not a Phase 84 blocker.

## Fail condition

A non-zero `duplicate_resolved` count **after `035`** means files the reconcile did not repair.
**Do not merge.** Return to
84-01 and widen the reconcile.

## Recording rules

- Record only the invariant TOTALS and the before/after counts. **Never paste the `--database-url`
  DSN** (it carries a password) into the SUMMARY, the PR body, or any committed log. `make_url`
  keeps the DSN password-safe in the tool's own output — do not defeat that by echoing it.
- The `completed` jump and `failed` drop in `get_fingerprint_progress` are **the fix, not a
  regression** (D-11). `completed` previously read `state == FINGERPRINTED`, whose sole writer is
  `retry_analysis_failed`, so it counted almost nothing; `failed` was a per-engine **row** count
  that double-counted files and misclassified one-success/one-failure files. Frame both deltas as
  intended in the SUMMARY so they are not read as breakage.

## To close

Run the runbook, then write `84-06-SUMMARY.md` with the recorded TOTALS and count deltas,
and mark the phase complete via `/gsd:execute-phase 84` (it will resume at the only
incomplete plan).
