# Parallel full-coverage gate decision

- **Bead:** `phaze-b9ylj.2`
- **Date:** 2026-09-11
- **Status:** GO, pending review of the spike molecule
- **Evidence:** [`phaze-b9ylj.1-parallel-full-coverage-gate.md`](phaze-b9ylj.1-parallel-full-coverage-gate.md)

## Decision

Proceed to `/bh:replan` for an implementation molecule for a **two-worker, order-preserving manual
shard runner** for the existing local non-browser full-coverage gate.

The measured candidate completed three consecutive green runs in 703.5 s, 693 s, and 708 s. Its
703.5 s median is 44.15% faster than the 1,259.60 s serial baseline. All runs preserved the exact
8,656-node union, 295 measured source files, 28,156 executed source lines, and 4,181 executed true
branch arcs.

Three workers and current pytest-xdist execution are outside the decision. The three-worker trial
changed process-global import preconditions and failed one test. xdist workers cannot currently
receive separate PostgreSQL application databases, migrations databases, and Redis logical
databases before their sessions begin.

## Replan contract

The implementation molecule must preserve these boundaries:

- two workers maximum, using the measured grouping of the seven checked-in CI buckets;
- the serial collection order is canonical, and execution fails before tests if lane union,
  uniqueness, or per-lane relative order differs;
- each worker receives all three exact exports from its own `just test-db-for` seat;
- coverage data, JUnit output, caches, temporary paths, and Beadhive reports are per worker;
- coverage combines only after both workers pass, and all child, combine, and report failures
  propagate;
- `INT` and `TERM` reach each lane's whole process group, all descendants are awaited, and both
  seats are released through the ordinary retrying release path on every exit; and
- contract validation proves node identity, coverage-line and true-branch equivalence, failure
  propagation, and cleanup on success, child failure, combine failure, and interrupt.

This GO does not itself change the test gate. The implementation beads should be filed from the
reviewed decision so their acceptance criteria cannot drift from the measured constraints.
