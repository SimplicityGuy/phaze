---
status: partial
phase: 79-shadow-compare-gate-live-corpus
source: [79-01-SUMMARY.md, 79-02-SUMMARY.md]
started: 2026-07-08T00:00:00Z
updated: 2026-07-08T00:00:00Z
---

## Current Test

[testing complete — 6/6 runnable tests passed; 1 blocked (deferred to homelab per D-02)]

## Tests

### 1. CLI is discoverable and self-documenting
expected: `python -m phaze.cli.shadow_compare --help` (and `just shadow-compare --help`) prints a usage line listing `--sample-cap`, `--verbose`, and `--database-url`.
result: pass
evidence: help output lists all three flags; `just shadow-compare` recipe shown in the `db` group with the MIG-02 doc string.

### 2. Clean corpus → exit 0
expected: Running the gate against a consistent (here: empty) corpus prints a per-invariant Report with every HARD invariant at 0 and `hard_fail_total=0`, and the process exits 0.
result: pass
evidence: `python -m phaze.cli.shadow_compare --database-url <phaze_test>` printed `TOTALS: hard_fail_total=0, soft_divergence_total=0`; exit code 0.

### 3. Hard divergence → exit 1
expected: With a file at `state=analyzed` and NO backing analysis row, the gate flags that invariant (count ≥ 1, sample file_id), reports `hard_fail_total ≥ 1`, and exits 1.
result: pass
evidence: after seeding the divergent row, output showed `[HARD] analyzed ... 1 divergent -- sample: 111ff889-...`, `hard_fail_total=1`; exit code 1.

### 4. Negative `--sample-cap` rejected before any DB opens
expected: `--sample-cap -1` fails argparse validation with a clear usage error and does not run a query.
result: pass
evidence: `argument --sample-cap: --sample-cap must be >= 0`; exit code 2 (argparse usage error). (Review fix WR-02.)

### 5. DSN password never leaked
expected: Passing a `--database-url` containing a password prints at most the host/db name — never the password — to stdout or logs.
result: pass
evidence: with `...:SUPERSECRET@localhost:5433/phaze_test`, output contained 0 occurrences of `SUPERSECRET` and printed only `shadow-compare: target database localhost/phaze_test`. (T-79-04 / review fix WR-01.)

### 6. `just shadow-compare` recipe drives the same core
expected: `just shadow-compare *ARGS` is a `db`-group recipe that invokes `uv run python -m phaze.cli.shadow_compare` and threads flags through.
result: pass
evidence: `just --list` shows the recipe in the `db` group; `just shadow-compare --help` forwarded through to the module's usage output.

### 7. Gate passes on a restore of the live ~200K-file corpus
expected: `just shadow-compare --database-url <restore-dsn>` against a restore of the live corpus (post-`032` backfill) exits 0, or exits 1 only on soft FINGERPRINTED/LOCAL_ANALYZING divergence with all HARD invariants at zero; output recorded in VERIFICATION.
result: blocked
blocked_by: prior-phase
reason: "Deferred to the next homelab rollout per CONTEXT decision D-02 — no live corpus dump is available to this worktree. Tracked as the sole Manual-Only check in 79-VALIDATION.md and in 79-HUMAN-UAT.md; must be recorded before Phase 90's destructive 033."

## Summary

total: 7
passed: 6
issues: 0
pending: 0
skipped: 0
blocked: 1

## Gaps

[none — 6/6 runnable behaviors passed; the single blocked item is a by-design deferral, not a code gap]
