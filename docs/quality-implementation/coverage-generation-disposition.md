# Coverage generation ownership: retain existing escalation

Task **phaze-a393q (SB)** records the installed-capability decision for implementation epic
**phaze-75qpj**. The approved assessment is PR #716, source
`75e586ef9d1d412dfcf8ecb34818ecdda2576e70`. This investigation used implementation base
`acbe642aac79ed8427d4a867166c7735af58d511` and installed **Repowise 0.55.0**, Python 3.14.

**Disposition: exclude immutable generation publication from the current adoption. Retain
existing selector authority and full escalation on missing, stale or untrusted evidence.**
There is no selector, importer, coverage-policy or validation-population change. This disposition
is the approved SB fallback; it does not claim that a future generation implementation is impossible.

## Installed capability and actual round trip

The probe initialized a task-owned, two-file synthetic repository with `repowise init --mode fast
--no-prose --no-workspace --no-seed`, mock embeddings, and editor/key setup disabled. Its store
was explicitly isolated with `REPOWISE_DB_URL`. No shared Phaze index was refreshed and no durable
checkout was switched. The fixture has a real executed function and a coverage.py test context,
`test_answer.py::test_answer|run`. Both the binary report and its XML report were ingested by the
installed CLI with `--strict`, then read back through `repowise coverage status`.

| Observed operation | Exit | Wall seconds |
| --- | ---: | ---: |
| Cold synthetic index initialization | 0 | 20.044296375010163 |
| Corrected index-status readback | 0 | 1.541348416940309 |
| Binary context ingest | 0 | 1.0992318750359118 |
| XML total ingest | 0 | 1.0615890410263091 |
| Paired coverage readback | 0 | 1.0146014579804614 |
| Same store, different absolute repository path | 0 | 1.018482750048861 |
| Unsupported `coverage status --generation` probe | 2 | 1.1451645840425044 |
| Unsupported `coverage status --immutable` probe | 2 | 0.8659000000916421 |

The paired readback reports **2 files, 2 covered of 5 lines, 1 test, 1 test/file pair**, no unmatched
paths, and the exact fixture SHA `1e842dcd713711955334c6ce09602530107fe36b` for both ingests and
index freshness. The synthetic 40% line figure measures this deliberately tiny probe, not Phaze
coverage or the complete canonical test population. The closed SQLite file was **823296 bytes**;
this is neither a production generation-size estimate nor an approved retention budget.

The same isolated store queried with a different absolute repository path reports
`indexed: false` despite process exit zero. Exit status alone therefore cannot authorize an
index: the repository-specific JSON must be checked. A separate repository record is needed for
a refresh seat; changing the DB location does not rebind repository identity.

Two probe setup errors were corrected: `status` takes a positional repository path, not `--path`
(the erroneous invocation exited 2 in 0.11431820807047188 seconds); coverage.py's default sysmon
core cannot provide this dynamic-context proof. The successful probe used `COVERAGE_CORE=ctrace`
and an isolated coverage configuration. The initial no-data attempt is not ingestion evidence.

## What the capability does and does not establish

The installed public CLI supports repository paths and an explicitly configured database URL.
Those primitives can isolate an index. They do **not** supply a verified immutable coverage
snapshot, manifest-bound repository/object identity, atomic local publication, reader pinning or
interruption rollback protocol. The installed `publish` command publishes on repowise.dev; it is
not a local evidence-generation operation and was only inspected with `--help`.

Installed source confirms that `coverage add` finds repository records by their absolute path.
`save_test_coverage` deletes and replaces the repository's rows on ingestion; aggregate coverage
is also stored by repository. Thus an ordinary store is mutable, not a generation merely because
it lives in a separate directory. The unsupported-flag probes confirm the flags are absent;
flag absence alone is not a proof that a wrapper could never implement the required protocol.

Repowise 0.55.0 now stamps ingestion from live HEAD before falling back to the indexed repository
head. The assessment's warning about older versions stamping only the indexed head must not be
repeated as an observed 0.55.0 defect. This improvement still does not certify the complete
source/index/report/provenance contract by itself.

A future implementation could build on the isolated-store primitives, extend the **existing**
importer/gate, and explicitly teach the selector to validate and pin a generation. That would
require the real interrupted-writer and concurrent-reader controls, all CI/artifact identity and
expiry refusals, canonical nodes/skips/contexts, and exact paired readback specified by the
approved assessment. None of those unbuilt generation controls is marked passing here. No
concurrent publication experiment or actual eligible-CI refresh was attempted, so production
build/import/publication/recovery costs, warm samples and retained-generation disk are **unmeasured**.

## Operational and downstream boundary

Keep `scripts/select_impacted_tests.py`'s current strict refusal and target-aware source movement
checks, its complete floor, and its existing proven changed-test facts. Do not redirect it to the
synthetic store, silently point a consumer at an unvalidated DB, relax evidence checks, or label an
unknown result selected coverage. Full validation remains the recovery when existing authority
cannot prove selection. No accepted-bead velocity or runtime saving follows from this disposition.

The existing CI importer temporarily changes its indexed checkout. It must not be invoked against
the durable checkout while readers are active. A shared refresh would need an explicitly coordinated
quiescent window and verified restoration; **no such window or refresh is scheduled by this task**.
The synthetic fixture/store is task-owned investigation material, not published selector evidence.

SC can proceed only as a provisional shadow preflight under the existing authority, or record its
own exclusion where generation ownership is a prerequisite. SD/SE cannot adopt a new selection
authority on the strength of this report. PD must retain SB's non-adoption and unmeasured costs;
G0 still requires the actual assembled canonical full gate, and A0 must assess real coverage,
consumer outcomes and the joined measurement evidence. The cohort decision remains separate.

## Retained controls and review

Existing importer, paired coverage gate and fast-selector controls passed **159 tests, 0 skips,
1 warning in 4.28 seconds** on SB's isolated test seat. They include wrong/failed/expired CI,
missing report halves, missing contexts, restoration after failure, coverage shortfalls and selector
refusals. They verify unchanged safeguards, not the unimplemented generation protocol. The `-q`
transcript omits the pytest database header, so it is not a retained header/lock attestation.

Raw CLI outputs, operation argv/exit/timings and the targeted transcript are retained at
`<scratch>/assessment-artifacts/phaze-a393q/`. Final exact-tree hooks, sanctioned validation and
fresh review are recorded separately by Beadhive; this report does not predeclare their outcome.
