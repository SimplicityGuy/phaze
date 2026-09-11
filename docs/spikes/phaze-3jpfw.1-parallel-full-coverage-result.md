# Parallel full-coverage gate result

- **Evidence bead:** `phaze-3jpfw.1`
- **Implementation molecule:** `phaze-yrap4`
- **Date:** 2026-09-11
- **Status:** accepted and integrated

## Result

The two-worker, order-preserving local coverage gate retained the serial gate's coverage while
cutting its wall time. The reviewed serial baseline took **1,259.60 s**. The accepted leaf run
took **696.62 s**, and the final integrated molecule run took **719.60 s**.
Those runs were 44.69% and 42.87% faster than the serial baseline, respectively.

The final molecule run partitioned the canonical non-browser inventory into exactly two nonempty
lanes:

| Lane | Selected nodes |
| --- | ---: |
| A | 4,865 |
| B | 3,824 |
| **Exact union** | **8,689** |

The lane union preserved canonical order, contained no duplicates or omissions, and the final
result was **8,685 passed and 4 skipped**.

## Coverage fidelity

The accepted parallel result matched the reviewed serial baseline's measured coverage sets:

| Coverage evidence | Serial baseline | Accepted parallel run |
| --- | ---: | ---: |
| Measured source files | 295 | 295 |
| Executed source lines | 28,156 | 28,156 |
| Executed true branch arcs | 4,181 | 4,181 |

The final combined report recorded **98.61% combined coverage**, **99.12% line coverage**, and
**96.25% branch coverage**. Every measured module remained at or above the 90% module line floor.

Each lane wrote isolated coverage data. After both lanes passed, the runner combined that data and
emitted `coverage.json` and `coverage.xml` with artifact generation configured not to enforce the
floor. It then enforced the 95% combined report floor and ran the repository coverage-floor check
once. This order preserved both report artifacts if later floor enforcement failed.

## Resource cleanup

Both lanes used stable, worktree-derived test seats with separate PostgreSQL application and
migration databases and separate Redis logical databases. After the run, the runner cleared and
released both seats through the ordinary release path. It awaited both process groups before
returning, and no lane or supervisor process remained.

No local path, host, database, Redis index, or seat identifier is retained in this record. The
quantities above come from the accepted `phaze-yrap4.3` bead evidence and the final
`phaze-yrap4` molecule result. This documentation-only bead did not rerun the full test suite.
