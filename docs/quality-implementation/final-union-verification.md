# Final assembled union verification (G0)

Task **phaze-47gba (G0)** is the mandatory assembled comparison for implementation epic
**phaze-75qpj**. It comes after the [optional pilot disposition manifest](pilot-disposition-manifest.md)
(PD) and before adoption review **phaze-8d5bs (A0)**. This document has two parts:

- **Developer evidence on the batch branch:** the six canonical A-row fault receipts, four narrow
  oracle-gap controls, and the two harness amendments those receipts needed.
- **The checklist for root's assembled `just check-all`:** what that gate must show.

**This document is not the G0 verdict.** The verdict is root's legitimate assembled `just check-all`
on the exact final tree, read from that run's own pytest summaries and coverage lines. Nothing below
was measured by a full suite. Every receipt is one bounded, targeted invocation. **No performance
gain is claimed.**

## Canonical repairs and their A-rows

All six canonical repair beads are closed (`merged`), and their recorded commits are ancestors of
main `0f7a81afc92807bbcf2138ca04c85b8a30d4de05`. None needed a formal replan.

| Row | Repair bead | Landed commit(s) | Repaired file | Primary consumer node |
| --- | --- | --- | --- | --- |
| A1 | `phaze-zjwp4` | `52cd6e1c` | `src/phaze/tasks/tag_write.py` | `tests/integration/test_tag_timeout_ownership.py::test_worker_timeout_keeps_writer_owned_until_actual_undo` |
| A2 | `phaze-ct6l1` | `441a816f`, `abe3de37` | `src/phaze/routers/tags.py` | `tests/integration/test_tag_partial_save_undo.py::test_partial_save_preserves_original_anchor_through_retry_and_undo[True]` |
| A3 | `phaze-83205` | `d0beaa17` | `src/phaze/services/tag_comparison.py` | `tests/integration/test_tag_review_revision.py::test_committed_revision_change_refused_before_durable_authorization[bulk]` |
| A4 | `phaze-33c4p` | `3b480023` | `src/phaze/tasks/controller.py` | `tests/integration/test_orphan_backend_scheduled_revisit.py::test_scheduled_cleanup_recovers_only_eligible_orphans[age]` |
| A5 | `phaze-jpm1y` | `3cf87558`, `16ded8dd` | `src/phaze/tasks/functions.py` | `tests/integration/test_analysis_admission_heartbeat.py::test_active_capacity_waiter_survives_real_sweep_and_completes_once` |
| A6 | `phaze-g7x85` | `20cd3d5c` | `src/phaze/services/provider_persistence.py` | `tests/integration/test_local_source_attribution.py::test_equal_interpretations_keep_each_raw_attribution[160000]` |

The optional-branch dispositions are the ones PD records. The adopted bytes that enter this union
are C5's two path controls and R4's shard rebalance. Every other pilot is excluded or deferred, and
that shrinks no population below.

## Fault receipt protocol

Each row ran three cells in strict sequence: **baseline**, **fault**, **restored**. Each cell was a
fresh `uv run pytest <exact-node> --rootdir=<worktree> --junitxml=<scratch>/<row>-<phase>.junit.xml
-p no:cacheprovider -x` process. No full suite, xdist or extra worker ran. All cells used one owned
seat, set up with the three exports from `just test-db-for` copied verbatim. All 18 cells printed
the header `phaze test database: '<seat>_test' on localhost:5433 (... exclusive)`, every one naming
this seat. No bypass was set.

An owned supervisor launched each cell in its own process group and recorded the following:

- argv, Python 3.14.7, seat database names, HEAD and tree, and the worktree's dirty paths before and
  after (empty at every cell).
- **Preimage check.** The target file's SHA-256 was compared to the planned repaired-source hash
  before any fault operator, and the cell refused on a mismatch. All six matched final source.
- **Fault operator.** Exactly one fault operator per fault cell. A row's fault operator preimage must
  occur exactly once (the cell refuses otherwise) and is applied before launch. The faulty SHA-256 and diff are recorded.
- **Restore.** The saved preimage is restored in `finally`, only after the child exited and was
  reaped. The file is then rehashed and re-diffed. Every cell restored byte-identically with an empty
  residual diff.
- **Wall bound and termination.** On expiry the group gets SIGTERM, then SIGKILL 5 s later, then a
  5 s reap. A terminated or killed cell can never count as green. No cell hit its bound: every
  `termination` reads `exited`, and no process in the group was left alive.
- **First failure.** Read from that cell's JUnit, together with the failing test source line.

A1 changes no source bytes. Its fault is a negative-only pytest plugin, scoped to the one exact node.
It replaces `phaze.tasks.tag_write.fcntl.flock` with a no-op in `pytest_runtest_setup` and restores
it in `pytest_runtest_teardown`. The plugin recorded that the original and restored callables are
the same object (`restored: true`), and process exit is the second isolation boundary. The baseline
and restored cells did not load the plugin.

Fault receipts (logs, JUnit XML, JSON receipts and the supervisor) stay in the batch worktree's
ignored per-bead scratch directory. They are not tracked. The table below copies their values
exactly, with synthetic test UUIDs replaced by placeholders.

## A1-A6 receipts

Source hashes are full SHA-256 values. "pytest" is each cell's own summary line. Wall time is the
supervised process's elapsed time, interpreter startup included.

| Row | Final-source preimage SHA-256 | Faulty SHA-256 | Operator | Wall bound |
| --- | --- | --- | --- | ---: |
| A1 | `89463e9f963e7325206c89e35485654874eb8728030e35b3e9a8451927047ee8` | n/a (runtime plugin; source unchanged) | node-scoped `fcntl.flock` no-op | 60 s |
| A2 | `7cc4d542b47c18fd0d42ecde0f7775fefb659d2b19949af9ce55642f89c2d583` | `f91c56eb715b23f4cd019d8ede05a850f9047a0ce3a91cf10726788eec9e2f22` | drop only `TagWriteStatus.SAVE_FAILED` from `_UNDOABLE_TAGWRITE_STATUSES` | 30 s |
| A3 | `0f256ddcde0a362378af2876d31f30ce221e6b27d744012f03fd5a83e8cfc198` | `caca3e42964e2071b15cb73644be084c9ff7de7bf625418f9d937098cfba5523` | delete only the reviewed-revision refusal `if` block | 60 s |
| A4 | `e4b8793157284427c6259d9ebd038bdc36a633b5d788e1dc33bbe8178b49c43a` | `a4098c2796025b8b2119b33e6879f2be5bdb31d302debedf9d9a60cc505bcd63` | delete only `CronJob(reap_orphaned_backend_cloud_jobs, cron="* * * * *")` | 60 s |
| A5 | `37bac28da5b74501276ac7388d4b0f55d5bd9ccfccc6887cdf46ae901d6fdf06` | `9fb744e96306063e3b4ac4195b6599e4fa6973fc93464e980497bec591131ce3` | waiting relay `task = asyncio.create_task(relay()) if ...` becomes `task = None` | 60 s |
| A6 | `85de17f8a6dcc14029a82c345a7d44ec7654fda3e60bcd51dcac8e3f186b746a` | `bb5d45c31ee17cb567d2abbbd1f2789c7f0308abc28ec166d0a8ace13cd958cc` | delete only the reuse-query `parent_id` constraint (two lines) | 60 s |

Every faulty hash also equals the hash derived before execution in private preparation notes. Those
notes are not a durable record; the hashes above are the values the supervisor measured.

| Row | Baseline | Fault (first semantic failure) | Cold restored |
| --- | --- | --- | --- |
| A1 | exit 0, 5.336 s; `1 passed, 10 warnings in 1.34s` | exit 1, 6.455 s; `1 failed, 10 warnings in 4.10s`. `test_tag_timeout_ownership.py:145`, `assert _raw_tags(path) == original`: the stale writer left `TPE1 ['New Artist']`, `TIT2 ['New Title']`, `TDRC ['2024']`, `TCON ['Rock']`, `TRCK ['7']` over the originals after the `finished`/`owner_settled` barriers | exit 0, 3.851 s; `1 passed, 10 warnings in 1.31s` |
| A2 | exit 0, 4.393 s; `1 passed, 10 warnings in 1.30s` | exit 1, 4.431 s; `1 failed, 10 warnings in 1.43s`. `test_tag_partial_save_undo.py:66`, final `_raw_tags(path) == original`: `{'TPE1': ['New Artist']} != {'TPE1': ['Orig Artist']}` after a real partial save, retry and actual Undo | exit 0, 3.486 s; `1 passed, 10 warnings in 1.14s` |
| A3 | exit 0, 4.756 s; `1 passed, 10 warnings in 1.62s` | exit 1, 4.16 s; `1 failed, 10 warnings in 1.50s`. `test_tag_review_revision.py:102`, `'1 could not be dispatched'` absent; the real bulk route rendered `2 awaiting approval` | exit 0, 4.313 s; `1 passed, 10 warnings in 1.55s` |
| A4 | exit 0, 13.045 s; `1 passed, 10 warnings in 4.95s` | exit 1, 7.323 s; `1 failed, 10 warnings in 3.49s`. `test_orphan_backend_scheduled_revisit.py:65` (helper, called from `:140`): `controller scheduling must revisit deferred retired-backend staging rows`, `assert None is not None`, after the real boot helper and snapshot assertions passed and the installed `worker.schedule()` ran | exit 0, 8.926 s; `1 passed, 10 warnings in 3.82s` |
| A5 | exit 0, 4.203 s; `1 passed, 1 warning in 0.29s` | exit 1, 5.477 s; `1 failed, 1 warning in 1.82s`. `test_analysis_admission_heartbeat.py:138`, the real `queue.sweep(abort=0.01)` returned `['<waiter-job-key>']`. The three recorded observations were each `observed=False` with `touched == origin`, at clock `origin + 1200000`, `+ 2400000` and `+ 3601000` ms, `heartbeat 3600`, `ACTIVE`, `attempts 1` | exit 0, 4.386 s; `1 passed, 1 warning in 0.64s` |
| A6 | exit 0, 4.963 s; `1 passed, 10 warnings in 1.55s` | exit 1, 3.228 s; `1 failed, 10 warnings in 0.94s`. `test_local_source_attribution.py:60`, the distinct raw captures shared one child interpretation id (`<uuid-1> == <uuid-1>` where `160000 == 140000` is False) | exit 0, 4.611 s; `1 passed, 10 warnings in 1.88s` |

Every fault failure is an assertion at the intended consumer oracle, reached after its real
prerequisites. None is an import error, collection error, skip or timeout. None of the 18 logs
contains a teardown or setup error, a destroyed pending task, an unawaited coroutine, an unclosed
resource or a SAQ shutdown-grace warning. A4 and A5 assert their own settlement in `finally`, and in
each fault cell the reported failure is still the semantic oracle. An `AssertionError` raised in a
`finally` replaces the body's exception, so that is evidence the settlement assertions passed there
too.

**A6 cell history.** A6's first fault launch stopped inside the supervisor before pytest started: a
transient `git write-tree` failure while it recorded provenance. The fault operator had not yet been
applied and the source was unchanged. The supervisor was hardened to retry a transient git read, and
A6's fault cell then ran once, followed by a fresh cold restored cell. The earlier restored green,
which never had a fault launched before it, was kept under a separate name and is not cited above.

### Later oracles each fault leaves unreached

A first failure shows only the statements before it ran under faulty source. These later checks
were **not** reached by the fault cells. They are proven only by the baseline and restored greens
and by the normal union:

- **A1:** the acknowledged digest/hash assertions after the raw-tag equality.
- **A2:** nothing after line 66 in this case. The `[False]` direct-Undo case is a separate control,
  not interchangeable first-failure evidence.
- **A3:** the zero-B-audit and capture SQL checks after the response assertion.
- **A4:** eligibility, recovery, candidate drain and idempotency after the missing cron job.
- **A5:** the positive three-observation assertion and both jobs completing once with the limiter at
  0/0. This primary does not cover actual Worker abort delivery; the separate admitted-stall test
  does.
- **A6:** the fresh `source_review_context` and classification checks after line 60.

## Harness amendments the receipts depend on

The amended test bytes were committed before any cell ran. Every cell bound the same HEAD, and
historical receipts from the older harnesses stay separate evidence.

**Where the bounds come from.** The approved
[assertion proposal](../quality-assessment/assertion-proposal.md), merged with the assessment in
PR #716, sets the containment budgets in its bounded-sample table (lines 175–176):

- A4: "Each await 5 s; settle scheduler within 10 s".
- A5: "controlled time advances 3,601 s beyond heartbeat 3,600", "At most three observed
  relay/sweep cycles per variant, 5 s event waits and 10 s Worker/queue shutdown".

The 3601 s figure itself traces to the original reproduction in the
[analysis hunt](../quality-assessment/analysis-hunt.md) (line 78), which advanced the installed SAQ
clock by 3601 seconds. **The exact forms below are the implementer's decision**, chosen to fit
those budgets:

- the A4 split into per-transition `asyncio.timeout(5)` blocks plus ONE 10 s aggregate cleanup
  budget;
- the A5 split into exactly three advances of 1200, 1200 and 1201 seconds;
- A5's single 5 s gather inside a 10 s aggregate cleanup.

The operator was not asked about these forms.

- **A4.** The test previously bounded only `Worker.process` at 10 s, which does not meet the
  proposal's "each await 5 s" budget. Every non-cleanup async transition now runs under
  `asyncio.timeout(5)`:
  - the whole schedule/read/process/read tick;
  - the boot helper and the snapshots;
  - broker connect/enqueue;
  - the seed, age-update and candidate session blocks.

  The `finally` settlement shares **one 10 s aggregate** budget: stop both workers, delete only this
  test's unique queue rows, then disconnect both pools. It then asserts that neither worker retains
  a task or job context. There is no production timeout change and no test-side reaper enqueue. All
  three `[age|live-submit|transient-broker]` cases passed under the new bounds (targeted module
  run), so no bound tripped. Per-transition durations were not recorded, so "under 5 s" is a
  ceiling, not a measurement.
- **A5.** The four `1201` s steps (4804 synthetic seconds) are replaced by exactly three advances,
  **1200, 1200, 1201 = 3601 s**, against the stored 3600 s heartbeat and SAQ's strict
  `age > heartbeat`. Further changes:
  - The immutable clock origin is recorded before the seams are patched.
  - These prerequisites are asserted before any advance: `original_touch == origin`, `ACTIVE`,
    `attempts 1`, `heartbeat 3600`, and limiter `borrowed 1 / waiting 1`.
  - Each step records one committed-PG observation (success flag, touched, heartbeat, status,
    attempts) without asserting it early.
  - The real sweep is the **first** semantic oracle, and its result is captured before cleanup can
    rewrite the broker row.
  - The three positive observations are asserted only **after** the real PG sweep.
  - Cleanup releases the held extraction and settles both process tasks under one 5 s gather,
    cancelling survivors. It then stops the worker, all within one 10 s aggregate budget, and only
    then lets `stage_env` delete the owned rows.

  The stored heartbeat, the 1800 s stall default and the six existing controls are unchanged.

## Oracle-gap controls

These four controls reuse existing strong tests and fixtures and keep real consumers. They add **6
nodes, rename or remove none**: collection of the six touched modules went from 124 nodes at
`0f7a81af` to 130, with no node removed.

| Gap | New node(s) | What it proves | Reused, not duplicated |
| --- | --- | --- | --- |
| Orphan refresh loop | `tests/shared/services/test_orphan_cache.py::test_orphan_refresh_loop_warns_on_a_failed_iteration_and_keeps_polling` | See "Orphan refresh loop" below | existing last-good and lifespan-cancel tests stay as they are |
| Undo `RecursionError` | `tests/review/routers/test_duplicates.py::test_undo_parser_recursion_error_is_a_named_422_and_leaves_resolutions_intact[<single-undo-path>]` and `[/duplicates/undo-all]` | See "Undo RecursionError" below | `_UNDO_PATHS`, `_extract_server_file_states`, `verify`; the native deep-input envelope test is unchanged |
| Cached trim `None` | `tests/analyze/services/pipeline/test_analysis_streaming_decode.py::test_a_cached_none_trim_reaches_the_real_decode_teardown` | See "Cached trim None" below | the tier-pass oracle, factored into a helper so the original node id `test_analyze_file_decodes_each_tier_in_exactly_one_pass` is unchanged |
| Orphan-details fault | `tests/shared/test_record_why_lines.py::test_orphan_details_fault_degrades_the_record_and_keeps_the_outer_session_usable[nested-entry]` and `[inside-savepoint]` | See "Orphan-details fault" below | `_pin_empty_saq_jobs`, `_seed_file`, the real client/session; the healthy diagnostics tests stay as they are |

- **Orphan refresh loop.** Drives the actual `phaze.main._orphan_refresh_loop` with the real
  `refresh_stage_orphan_counts`. The first iteration raises, the second succeeds, and the third
  parks until cancellation, which proves the loop kept polling. Exactly one named WARNING
  (`orphan refresh loop iteration failed; keeping last-good`) carries the cause. The cache read
  during the second iteration still holds the last-good value; afterwards it holds the fresh value.
  Iterations are gated by the compute calls, with no timing sleeps.
- **Undo RecursionError.** Forces `RecursionError` from `request_guards.json.loads` for **one
  sentinel**: the real server-rendered payload. All other input delegates to the saved original.
  Both real routes return 422 with `file_states is nested too deeply to parse`. The committed
  resolution marker is unchanged, and the same payload then really undoes, which proves it was a
  live, mutating payload.
- **Cached trim None.** Forces the facade's cached `_MALLOC_TRIM` to `None` and runs the real
  `analyze_file` decode. Every decoder trim the teardown and chunk boundaries make (at least 4)
  received `None`. The decoder's own memo is a recording callable that is never called, on macOS
  and glibc alike, so the test discriminates on both. The decoder trim is observed and delegated,
  not mocked.
- **Orphan-details fault.** The `nested-entry` arm raises at `begin_nested`, before any SAVEPOINT
  exists. The `inside-savepoint` arm replaces the first `scheduling_ledger` read with SQL that
  Postgres really rejects (`division by zero`) inside the SAVEPOINT. In both arms:
  - the mapping is all-`None` and exactly one `file_orphan_details_degraded` WARNING names the cause;
  - the real record route returns 200, so every later reader on the same session ran;
  - the committed ledger row is still readable on that session;
  - with the fault removed, the diagnostics render again.

  The fault is scoped to the route's single orphan-details call.

**Targeted results on the owned seat:**

- The four control modules plus both amended integration modules, at the committed test bytes:
  `130 passed, 10 warnings in 37.34s`. The first run surfaced test-authoring defects only: a
  structlog `exc_info` assumption, a bulk-canonical assumption, and a `verify`-session savepoint
  nesting. All were fixed in test code.
- The final verbose run of the 7 control nodes: `7 passed, 10 warnings in 5.76s`.
- The streaming-decode module plus both recursion nodes, after the node-id fix: `27 passed`.
- Shard-manifest and fast-gate guards: `140 passed, 1 warning in 3.22s`.

These counts belong to those runs only.

**Observed production defects: none.** No control or fault receipt observed a semantic failure in
current production source, and no production file was changed. Each gap was a missing
fault-consumer oracle, not a demonstrated bug.

## What root's assembled `just check-all` must show

G0 is complete only when root's legitimate assembled gate on the exact final head/tree supplies the
following, from its own output:

1. **The provider verdict for the exact SHA and tree.** It must be a real run. A `validation verdict
   reused (...)` replay is not one.
2. **Actual ordered, disjoint node manifests for both lanes and the lane JUnits.** Their union must
   equal the canonical collection: exact identity and multiplicity, with no node in both lanes and
   none in neither. A zero-test wrapper JUnit is never the lane population.
3. **Population accounting against the latest matched baseline.** The R4 CI head executed 11320
   nodes with 15 skips. The final local population must be explained from that number plus this
   branch's **+6** nodes, with skips accounted for by name. Any local-only or CI-only parameter
   difference must be identified by its platform-dependent parameter source; a count delta is not an
   explanation.
4. **Context-bearing binary coverage plus its JSON and XML halves.** Combined from the run's own lanes.
5. **Source-sensitive covered and missing line/arc sets with a denominator explanation**, set against
   the denominators the [accepted increment](accepted-increment.md) and the
   [coverage baseline](../quality-assessment/coverage-baseline.md) document. Any lost covered line
   or context needs an explanation.
6. **Overall LINE coverage strictly above 95%**, with the 90% per-module floor and existing true
   branch checks unchanged. No exclusion, floor waiver or padding.
7. **The six A-row primaries and all their existing controls in the normal population**, green, plus
   the six new control nodes.

## Limitations

- **One combined branch arc is a known gap, not an R4 regression.**
  `src/phaze/routers/pipeline/backfill.py:114 -> 121` (the "live keys present, none matching" edge)
  was covered at the R4 baseline only through cross-test state, and is uncovered at the R4 head (see
  the PD manifest). It is tracked as its own bead and is out of G0's scope. G0's branch result must
  carry it as a stated, pre-existing gap. It must not be explained away, and it must not be fixed
  inside G0.
- **The fault cells collected no coverage.** Their receipts prove detection and restoration, not
  coverage. They do not replace the final full coverage, and partial fault coverage is not presented
  as gate coverage.
- **The A4/A5 bounds are ceilings that never tripped.** Slow cancellation can outlive a coroutine
  bound, which is why the owned subprocess supervisor and its termination record are part of each
  receipt.
- **A5's historical 4x1201 receipts are separate.** They are not restated as 3601.
- **A5 is now stricter, and could flake under heavy load.** Before, a waiter-touch observation that
  missed its 0.5 s polling window was simply suppressed. Now each of the three steps MUST have
  observed the committed touch within 0.5 s, asserted after the sweep. That is a real timing
  requirement on relay cadence and PG commit visibility. On a heavily loaded host it could fail
  without any regression. A red on that assertion alone, with the sweep oracle green, is a
  limitation to investigate under load before reading it as a relay defect.
- **Fault receipts are local scratch.** They do not outlive the batch worktree. This document copies
  the values that matter.
- **No runtime, velocity or coverage-percentage claim** follows from any receipt here. A0 decides
  from root's gate and the PD record.
