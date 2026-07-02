# Phase 63: Parallel CI & Code-Change Gating - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-02
**Phase:** 63-parallel-ci-code-change-gating
**Areas discussed:** Sharding mechanism, Bucket selection, Integration tests placement, Change-gate scope

> Note: an initial pass of these questions was auto-accepted by a stray click in Orca and
> discarded; all four were re-asked and answered deliberately. This log reflects the
> deliberate answers.

---

## Sharding mechanism (CI-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Job matrix | One GitHub job per bucket (~8). Max wall-clock, satisfies CI-01 literally, free public-repo minutes; coverage combined from artifacts. | |
| pytest-xdist only | One job, `-n auto`. Simplest/cheapest, auto-combines; bounded by one runner, buckets not separately visible. | |
| Hybrid matrix + xdist | Matrix over buckets, `-n auto` inside each. Most parallelism; combine across jobs AND xdist workers. | ✓ |

**User's choice:** Hybrid matrix + xdist
**Notes:** Wants both the per-bucket CI visibility (matrix) and intra-bucket parallelism (xdist).

---

## Bucket selection (CI-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Auto-mark by path in conftest | Extend existing conftest auto-marker; zero per-file tagging, no reorg. | |
| Explicit markers per file | Hand-tag all ~212 files; explicit but error-prone. | |
| Directory reorg + path globs | Move test_*.py into tests/<bucket>/ dirs; select by path. Structurally exclusive partition. | ✓ |

**User's choice:** Directory reorg + path globs
**Notes:** Chosen over auto-marking (my initial recommendation). Payoff: a file in exactly
one dir makes the partition structurally exclusive — trivially trustworthy for CI-03.
Builder's-call added: shared helpers + root conftest.py stay at tests/ root; only test_*.py
files relocate; add a partition-guard test for files outside bucket dirs.

---

## Integration tests placement

| Option | Description | Selected |
|--------|-------------|----------|
| Dedicated integration bucket/job | One job owns `-m integration` + provisions postgres+redis; unit buckets service-free. | ✓ |
| Postgres in every shard | Uniform but N× startup cost; most buckets never touch the DB. | |
| Per-step integration split | Integration tests bucket with their workflow-step; scatters DB setup. | |

**User's choice:** Dedicated integration bucket/job
**Notes:** Reuses the proven green service setup from tests.yml; its .coverage still folds
into the combined report.

---

## Change-gate scope (CI-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Keep + formalize/test | Leave md-only skip logic, add regression tests + document contract. | |
| Broaden the skip rule | Explicitly treat .planning/**, LICENSE, other non-source as skippable; lock required-check contract. | ✓ |
| Leave entirely as-is | Consider CI-04 done; risk requirement not re-verified. | |

**User's choice:** Broaden the skip rule
**Notes:** CI-04 is ~80% built (detect-changes + aggregate-results skip-with-success on
md-only). Broaden the classifier and keep the changed-files gate job (not bare paths-ignore)
to avoid the "required check never runs" trap.

---

## Claude's Discretion

- Matrix YAML shape, artifact names, `coverage combine` invocation, single-vs-per-bucket Codecov flags on the one combined upload.
- `-n auto` vs fixed worker count; verify xdist multiprocessing combine works with the `greenlet, thread` coverage concurrency setting.
- `just` recipe names/signatures for per-bucket runs and coverage combine.

## Deferred Ideas

- Coverage gate raise + per-module uplift → Phase 64 (COV-01/02).
- REQUIREMENTS traceability gate / `/saq` re-link / dead-code sweep → Phase 66.
- pytest-cov → other tooling swap → out of scope (REQUIREMENTS out-of-scope table).
