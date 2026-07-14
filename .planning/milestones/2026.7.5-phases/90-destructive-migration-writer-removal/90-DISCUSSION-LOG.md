# Phase 90: Destructive Migration & Writer Removal - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-12
**Phase:** 90-destructive-migration-writer-removal
**Areas discussed:** Downgrade strategy, Migration self-guard, PR / blast-radius split, Standing anti-drift guard (+ a mid-discussion scope discovery: surviving live state-readers)

---

## Downgrade strategy — posture

| Option | Description | Selected |
|--------|-------------|----------|
| Best-effort backfill | Recreate column+index, backfill representative FileState per file from derived markers, docstring enumerates lossy cases | ✓ |
| Structural + documented-lossy | Recreate column with DISCOVERED default, no data backfill, docstring explains why | |
| Raise NotImplementedError | Forward-only (Phase 89 D-10 posture), prose-only reconstruction map | |

**User's choice:** Best-effort backfill.
**Notes:** Honors MIG-04's literal wording ("downgrade reconstructs the enum from derived sources"). Supersedes the Phase 89 D-10 NotImplementedError precedent.

## Downgrade strategy — collapse precedence

| Option | Description | Selected |
|--------|-------------|----------|
| Furthest-along pipeline stage | Walk the original linear order, pick most-advanced stage reached | ✓ |
| Terminal-status priority | Prioritize authoritative terminal markers first, furthest-along only for in-flight | |
| You decide (document in docstring) | Planner defines from design §6.1 | |

**User's choice:** Furthest-along pipeline stage.

## Downgrade strategy — off-ladder / unreconstructable states

| Option | Description | Selected |
|--------|-------------|----------|
| Markers override ladder; transients lost | Durable markers (analyze-fail, dedup, rejected) win over rank; transient states collapse to nearest durable stage, each named lossy; round-trip test pins only durable cases | ✓ |
| Pure ladder, markers as rungs | Fold markers into one ordered ladder, always max rank | |
| You decide (enumerate in docstring) | Planner defines from design §6.1 | |

**User's choice:** Markers override ladder; transients lost.

---

## Migration self-guard — posture

| Option | Description | Selected |
|--------|-------------|----------|
| Guard, but only when data exists | Abort on mid-flight rows or failed shadow-compare invariants; skip cleanly on empty/fresh DB (avoids 038 footgun) | ✓ |
| Trust the runbook (no guard) | 039 just does DDL; drain + shadow-compare-green is operator precondition | |
| Guard on mid-flight only | Abort on mid-flight PUSHING/uploading only; shadow-compare stays external gate | |

**User's choice:** Guard, but only when data exists.
**Notes:** Explicitly must avoid repeating Phase 89 CR-02 (038 wrongly aborted on a fileserver-less fresh DB).

## Migration self-guard — implementation

| Option | Description | Selected |
|--------|-------------|----------|
| Inline sync SQL in the migration | Re-express shadow-compare invariants as plain sync SQL inside upgrade(); self-contained, no app coupling | ✓ |
| Import & reuse Phase 79 check | Single source of truth, but couples versioned migration to mutable app code | |
| Mid-flight guard inline; shadow-compare external | Inline only cheap mid-flight check; keep shadow-compare as external CI/operator gate | |

**User's choice:** Inline sync SQL in the migration.
**Notes:** A versioned migration must be frozen-in-time; accepts SQL duplication with the Phase 79 service as the cost of decoupling.

---

## PR / blast-radius split — structure

| Option | Description | Selected |
|--------|-------------|----------|
| Split: writers PR, then destructive PR | PR-1 remove dead writers (column present, safe); PR-2 destructive drop + enum delete | ✓ (then refined to 3 PRs — see below) |
| Single finale PR | One PR does writers + migration + enum deletion together | |
| You decide | Planner chooses | |

**User's choice:** Split (writers → destructive).

## PR / blast-radius split — PR-1 scope

| Option | Description | Selected |
|--------|-------------|----------|
| Pure writer removal only | PR-1 deletes only the ~9 .state= writes (+ dead imports/branches); column/index/mapping/enum 100% intact | ✓ |
| Writers + model default cleanup | PR-1 also drops the Python default=FileState.DISCOVERED | |
| You decide | Planner sets boundary | |

**User's choice:** Pure writer removal only.

## PR / blast-radius split — reader-cutover absorption (refinement after scope discovery)

| Option | Description | Selected |
|--------|-------------|----------|
| Three PRs: writers → readers → destructive | PR-1 writers; PR-2 convert surviving live state-readers to derived; PR-3 destructive drop + enum delete | ✓ |
| Two PRs: writers+readers → destructive | PR-1 writers AND readers (bigger, all reversible); PR-2 destructive | |
| You decide | Planner sizes boundaries | |

**User's choice:** Three PRs: writers → readers → destructive.
**Notes:** Triggered by a mid-discussion codebase-scout finding that design §7's "readers all cut over" claim is stale — `get_analyze_stage_files`, `get_pushing_count`, `get_pushed_count`, `get_analysis_failed_count/_files`, the pipeline.py:1707 query, and `analyze_workspace.html` f.state comparisons all still read `files.state` and power live dashboard cards. They must be converted before the column can drop.

---

## Standing anti-drift guard

| Option | Description | Selected |
|--------|-------------|----------|
| Type checker + a thin repo guard | Deleted symbols fail mypy/ruff; add ONE mutation-tested source-grep test forbidding reintroduction | ✓ |
| No dedicated guard test | Trust mypy/ruff + deleted symbols | |
| Full behavioral anti-drift suite | Also assert column absent from DB schema + models don't map it | |

**User's choice:** Type checker + a thin, mutation-tested repo guard.
**Notes:** A GREEN anti-drift test proves nothing unless mutation-tested (project memory feedback_mutation_test_guard_tests) — add fake `.state=`, watch RED, restore.

---

## Claude's Discretion

- `039` revision number (mechanically next after `038`).
- Backfill `UPDATE` batching/lock strategy over the ~11,428-file prod corpus.
- Abort-message wording + lossy-case docstring prose.
- Precise PR-2 reader-conversion boundaries (delete-as-dead vs convert-to-derived per function) once research enumerates the full surviving-reader surface.

## Deferred Ideas

None — discussion stayed within phase scope.
