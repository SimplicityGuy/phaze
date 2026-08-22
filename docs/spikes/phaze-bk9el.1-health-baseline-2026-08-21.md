# phaze-bk9el.1 — per-file baseline for epic phaze-bk9el, with the reachable / history-derived split

- **Bead:** `phaze-bk9el.1` (root bead of epic `phaze-bk9el`, "Code quality iteration 2")
- **Date:** 2026-08-21 (local). Timestamps in the console output below are UTC, so the ingest
  reads `2026-08-22 00:17` — same session, not a later one.
- **Baseline commit:** `65e5af46e5812969445b1d6c4aaf09a8b1a4b5d7` (`wt/bead/epic/phaze-bk9el`, the branch every wave-2
  bead forked from) — this is the fixed point `phaze-bk9el.19`'s ledger diffs against.
- **Status:** measurement only. No file under `src/` is modified by this bead.
- **Provenance:** every health figure here is a direct sqlite read of `.repowise/wiki.db`, written
  by this bead's own `repowise health` fold. Nothing came from the MCP tools. Cross-checked against
  two other routes with zero mismatches — see §9, which also *measures* why the staleness
  `phaze-bk9el.9` reported is not an MCP cache.

## What this is, and what it is for

`repowise health` prints the worst 20 files. Every wave-2 and wave-3 bead in this epic has to
report a before/after number for the specific files it touched, and `phaze-bk9el.19` has to
assemble those into one honest ledger. This artifact is the "before" side: the full per-file
metric and finding set at a named commit, plus the branch-coverage baseline the per-bead gate
(`just branch-check`, built by `phaze-bk9el.21`) compares against.

Two machine-readable files are committed beside this one:

| File | Produced by | Consumed by |
| --- | --- | --- |
| [`phaze-bk9el.1-health-baseline-2026-08-21.json`](./phaze-bk9el.1-health-baseline-2026-08-21.json) | `scripts/health_baseline.py` | `phaze-bk9el.19` (the ledger) |
| [`.coverage-baseline.json`](../../.coverage-baseline.json) | `just branch-check --write-baseline` | `just branch-check` in all 12 wave-2 beads |

## 1. Coverage was re-ingested BEFORE this snapshot (criterion 1)

`repowise update` does **not** re-ingest coverage.py data. It advances the structural index and
leaves `coverage_files` exactly as it found it. That is not a cosmetic gap: after `phaze-vu88k`'s
refactors, five files acquired a new `untested_hotspot` finding and a lower score purely because
the index had moved and the coverage had not — a stale-data artifact read as a regression. This
epic's numbers are not allowed to be read through the same artifact, so the coverage was
re-measured and re-ingested first, and the evidence is recorded below rather than asserted.

The path is the five-step one encoded in `scripts/repowise-coverage.sh` (`phaze-2rgq2`), which
exists because two of its steps are invisible from the tools' own output: `repowise coverage add
.coverage` populates only the per-test map and leaves `line_coverage_pct` NULL while every surface
reports success, and `repowise coverage add` exits 0 even when it maps nothing.

```console
$ repowise update                                        # step 1, at 8d1bf08c
$ repowise coverage add .coverage                        # step 3 — the per-test map ONLY
Built the test-to-code map: 31150 test->file record(s).
$ repowise coverage add coverage.xml                     # step 4 — per-file line coverage
Ingested coverage for 258 file(s) (258 exact, 0 resolved).
$ repowise health                                        # step 5
3290 marker findings
$ uv run python scripts/repowise_coverage_gate.py --coverage-status ... --index-status ... \
      --coverage-xml coverage.xml --started-at "2026-08-22 00:17:03" --expected-commit 8d1bf08c
✅ repowise coverage refresh verified at 8d1bf08cf1bd: 258 file(s) at 98.78% line coverage,
   31150 test->file pair(s) from 13495 test(s).
```

**Ingested counts, as criterion 1 requires them recorded:**

| Quantity | Value |
| --- | ---: |
| Test-to-code records (`.coverage`, per-test contexts) | 31150 |
| Distinct test records the gate counted behind that map | 13495 |
| Files with per-file line coverage (`coverage.xml`) | 258 (258 exact, 0 resolved) |
| Files in the Cobertura report | 258 — **all mapped**, so no `phaze-2rgq2` trap-3 partial |
| `health_file_metrics` rows for `src/phaze/**` | 382 (258 `.py`, the rest templates/markdown repowise also tracks) |
| — with `line_coverage_pct` populated | 258 |
| — with `branch_coverage_pct` populated | 181 (the modules that have branches at all) |
| Health findings rewritten by step 5 | 3290 repo-wide, 1128 on `src/phaze/*.py` |
| Suite behind all of it | 7365 passed, 2 skipped, 0 failed, 19:24 |

The 13495 figure is quoted from the gate verbatim and is **larger than the 7365 tests this run
executed**: repowise counts test records accumulated in the index, not tests in one session, so it
is not a check on the run and should not be read as one. The 258-of-258 exact mapping is the line
that matters. `repowise coverage add` reports an
unmapped remainder as a **note** and still exits 0 — a run that mapped 1 file of 249 once looked
entirely healthy on every surface — so "258 exact, 0 resolved" plus the gate's independent re-read
of the stored state is the evidence that the ingest is real, not the exit code.

`untested_hotspot` findings after the refresh: **0**. That is the biomarker that fired spuriously
on five files during `phaze-vu88k` because the index had advanced and the coverage had not; it is
absent here, which is the direct check that this epic is not reading through the same artifact.

### Where this run deviated from `just repowise-coverage`, and why

`scripts/repowise-coverage.sh` runs the suite *in the durable checkout*, because that is the only
directory holding a `.repowise/` index (the index is gitignored and keyed on the checkout's
absolute path, so a `bh` worktree has none and cannot be given one cheaply). Running it there
would have meant detaching the shared clone onto this bead's commit for the length of a full
coverage run, with fifteen agents live in worktrees cut from it.

So the two halves were split: the suite ran **in this bead's worktree** at the baseline commit,
and its two artifacts were ingested **in the durable checkout**. That is sound here for a reason
specific to this epic's shape, and the reason is checkable rather than argued:

```console
$ git rev-parse 65e5af46:src 8d1bf08c:src
dff673ae9e1e19c7579fbf96de796cd39e9cd903
dff673ae9e1e19c7579fbf96de796cd39e9cd903
```

The two commits name the **same `src` tree object**. Everything `phaze-bk9el.21` and
`phaze-bk9el.23` added lives in `justfile`, `scripts/`, `tests/`, `pyproject.toml` and `CLAUDE.md`;
not one byte under `src/` differs. Repowise's findings carry line numbers and coverage carries line
numbers, and the commit-pairing rule in `repowise-coverage.sh` exists to keep those describing the
same bytes — identical tree objects is a stronger guarantee of that than equal commit shas, not a
weaker one. `relative_files = true` in `[tool.coverage.run]` means both artifacts carry
repo-relative paths, so they map into the index regardless of which directory measured them.

The check was re-run immediately before the ingest and again after it; both shas are recorded
above and in the JSON.

## 2. Reachable vs history-derived (criteria 2 and 3)

The headline score is dominated by biomarkers computed from **git history**, which no refactor can
touch. A file can have every complex method extracted, every clone removed and every bare `except`
narrowed, and still score badly because it was fixed 26 times in the last year. A ledger that
reports only the score therefore reads as "the refactoring achieved nothing", which is false and
also demoralising. Splitting the deduction is what makes it honest.

| Bucket | Biomarker types | Reachable by refactor? |
| --- | --- | --- |
| **reachable** | `dry_violation`, `error_handling`, `primitive_obsession`, `complex_method`, `nested_complexity`, `large_method`, `bumpy_road`, `low_cohesion`, `coverage_gradient` | yes — this is the epic's target surface |
| **history-derived** | `prior_defect`, `function_hotspot`, `change_entropy`, `co_change_scatter`, `knowledge_loss`, `churn_risk`, `hidden_coupling`, `ownership_risk` | no — decays only as the file stays stable |
| **performance** | `io_in_loop`, `serial_await_in_loop`, `nested_loop_with_io`, `hot_path_sync_io`, `membership_test_against_list_in_loop` | in principle, but this epic does not target them (`phaze-vu88k.2` did) |
| **unclassified** | everything else — currently `complex_conditional`, `brain_method` | undetermined; reported separately, never folded |

The last two rows are the ones that make the totals reconcile. The epic named nine reachable and
eight history-derived biomarker types; the index emits 23 distinct types on `src/phaze` alone. Folding the residue
into whichever bucket flatters the headline is exactly the move `phaze-vu88k.1` refused, and this
artifact refuses it the same way — the residual buckets are small, named, and separately totalled,
so a reader can verify that `reachable + history_derived + performance + unclassified` equals the
total deduction exactly.

**"Total deduction" is a sum of `health_impact`, not `10 − score`.** Repowise scores each dimension
separately and the headline is not a flat subtraction, so this is a faithful attribution of finding
impact across buckets — the right instrument for comparing buckets against each other, which is the
question the epic asks. It is not a claim that removing X points of reachable deduction raises the
score by X.

### The headline split

Over the 258 `src/phaze/*.py` files, 1128 open findings, total deduction **473.248**:

| Bucket | Deduction | Share | Findings |
| --- | ---: | ---: | ---: |
| **reachable** | 198.041 | **41.85%** | 623 |
| **history-derived** | 273.170 | **57.72%** | 409 |
| performance | 0.000 | 0.00% | 91 |
| unclassified | 2.037 | 0.43% | 5 |
| **total** | **473.248** | 100.00% | **1128** |

Two corrections to the working assumption this epic was scoped under, both measured rather than
estimated:

**1. History-derived is 57.72%, not ~65%.** On the face of it that is seven points in the epic's
favour — but read it together with the next section before banking it. 24.0 of the reachable points
are a measurement artifact, and removing them puts the genuinely reachable share at 36.78% against
the ~35% the "~65% history-derived" assumption implied. So the assumption was very nearly right,
and the seven-point gain is almost entirely spent on the artifact rather than on real work. What
survives either way:
`prior_defect` alone (140.896, 29.77% of everything) outweighs the three largest reachable
biomarkers combined (`complex_method` 60.570 + `coverage_gradient` 31.045 + `nested_complexity`
29.592 = 121.207).

**2. Performance contributes 0.000, on 91 findings.** Every `io_in_loop`, `serial_await_in_loop`,
`hot_path_sync_io` and `nested_loop_with_io` finding on `src/phaze` carries `health_impact = 0.0`
in this index. They are reported, they are real, and they deduct nothing — `phaze-vu88k.2` already
cleared the ones that scored. A bead that "fixes performance findings" to raise a score will
measure no movement, and that is a property of the scoring model, not of the fix.

### The reachable number has a floor inside it: 24.0 points nothing can reach

**12 empty `__init__.py` files carry a `coverage_gradient` finding worth 2.0 points each — 24.0
points, 12.1% of the entire reachable bucket and 77.3% of all `coverage_gradient` deduction — and
no test can remove any of it.** Cobertura reports `line-rate="0"` for a file with zero statements,
so repowise ingests 0.0% line coverage for `src/phaze/__init__.py` and its eleven siblings, while
`coverage.json` reports them at 100% (`num_statements: 0`). The finding's own `details_json` says
`{"line_coverage_pct": 0.0, "uncovered_fraction": 1.0, "deduction": 4.0}`.

This is a measurement artifact of the report format, not a gap in the tests, and it is the single
largest false lead in the reachable bucket. `phaze-bk9el.19` should subtract it before quoting a
reachable ceiling: **the genuinely reachable deduction is 174.041, not 198.041 — 36.78% of the
total, not 41.85%.** Any bead that goes looking for coverage to add in `src/phaze/enums/__init__.py`
is chasing this artifact.

The full list, each at exactly 2.000: `src/phaze/__init__.py`, `agent_watcher/__init__.py`,
`enums/__init__.py`, `prompts/__init__.py`, `routers/__init__.py`, `schemas/__init__.py`,
`scripts/__init__.py`, `services/__init__.py`, `tasks/__init__.py`, `tasks/_shared/__init__.py`,
`utils/__init__.py`, `web/__init__.py`.

## 3. Repo-wide coverage at the baseline commit

Measured on the full suite at `65e5af46`, with `branch = true` (enabled by
`phaze-bk9el.21`):

| Metric | Covered / total | Percentage |
| --- | ---: | ---: |
| Statements | 17554 / 17771 | **98.7789%** |
| Branches | 3684 / 3866 | **95.2923%** |
| Combined (`covered_lines + covered_branches` over `num_statements + num_branches`) | 21238 / 21637 | **98.1559%** |

These reproduce `phaze-bk9el.21`'s figures exactly, from an independent full-suite run at this
bead's own commit.

Three metrics, not one, because `branch = true` makes them genuinely different numbers and
different gates read different ones: `scripts/coverage_floor.py` enforces 95% repo-wide and 90%
per module on **statements**, while coverage.py's own `fail_under` reads the **combined** figure.
`.coverage-baseline.json` records all three per module for exactly this reason.

**Per-module, against `scripts/coverage_floor.py`'s 90% floor.** 246 of the 258 reported modules
carry statements (the other 12 are empty `__init__.py` files). Measured at this commit:

| Verdict | Modules below 90% |
| --- | --- |
| Statements (what `coverage_floor.py` enforces) | **0** |
| Combined (what coverage.py's `fail_under` reads) | **1** — `src/phaze/routers/duplicates.py`, 88.59% |
| Branches (gated per-bead only, never repo-wide) | 30 |

Exactly **one** module changes verdict between the statement and combined metrics, and it is
`src/phaze/routers/duplicates.py`: 91.20% statements, 75.00% branches, 88.59% combined. That single
module is the whole reason `phaze-bk9el.21` moved the per-module floor onto `percent_statements_covered`
rather than leaving it on the combined figure — left as it was, turning branch coverage on would
have failed a module that had regressed nothing.

The 30 modules below 90% on branches are why there is **no** repo-wide branch floor. They are not
a backlog this epic created and not one it is expected to clear; the per-bead gate
(`just branch-check`) only requires that a bead not *lower* the figure on a file it touched.

## 4. The DRY inventory (criterion 4)

Every `dry_violation` finding on a `src/phaze/*.py` file, with the clone partner and shared line
count taken straight from the index's `details_json`, so the DRY beads do not re-derive them.
Sixty-seven findings. (The JSON's repo-wide `dry_violation_count` is 68: the extra one is on
`src/phaze/static/js/analysis_timeline.js`, which is under `src/` but is not Python and is out of
scope for every bead in this epic.)

**What repowise does and does not record.** Each finding carries ONE partner — the *worst* clone
pair — alongside `clone_pair_count`, the number of pairs that finding summarises. Where the count
is greater than 1 the other partners are **not in the index at all**. This table reports what
exists; a bead working a finding with `clone_pair_count > 1` should expect to find further clones
that are not named here, and should not read a single-partner row as proof there is only one.
**52 of the 67 findings are in exactly that position**, so the incomplete case is the normal one.

Two more shapes a DRY bead will meet in this table and should not misread:

- **14 findings name the file itself as its own worst clone partner** — intra-file duplication, not
  a cross-module clone. `src/phaze/main.py` is the extreme case: 71 shared lines against itself
  across 6 pairs, 35.99% of the file.
- **Some partners are outside `src/`.** `src/phaze/models/__init__.py`'s worst partner is a test
  module, over 53 pairs — a re-export barrel matching every test that imports the same set of
  names. Deduplicating a production barrel against its tests is not a refactor; treat that row as
  a property of the biomarker, not as work.

| File | Lines | Function | Clone partner | Shared lines | Pairs | dup% of file | Impact |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: |
| `src/phaze/cert_bootstrap.py` | 115–136 | — | `src/phaze/cert_bootstrap.py` | 22 | 2 | 19.60 | 0.150 |
| `src/phaze/database.py` | 21–45 | — | `src/phaze/tasks/controller.py` | 25 | 1 | 26.60 | 0.350 |
| `src/phaze/enums/stage.py` | 136–148 | — | `src/phaze/enums/stage.py` | 13 | 2 | 12.62 | 0.150 |
| `src/phaze/main.py` | 238–298 | — | `src/phaze/main.py` | 71 | 6 | 35.99 | 0.350 |
| `src/phaze/models/__init__.py` | 4–18 | — | `tests/discovery/services/test_scan_deletion.py` | 15 | 53 | 38.60 | 0.600 |
| `src/phaze/models/agent.py` | 29–36 | — | `src/phaze/models/analysis.py` | 29 | 2 | 25.00 | 0.350 |
| `src/phaze/models/analysis.py` | 23–61 | — | `src/phaze/models/metadata.py` | 39 | 13 | 81.82 | 0.350 |
| `src/phaze/models/cloud_budget.py` | 90–102 | — | `src/phaze/models/cloud_job.py` | 29 | 1 | 13.98 | 0.150 |
| `src/phaze/models/cloud_job.py` | 90–118 | — | `src/phaze/models/cloud_budget.py` | 29 | 4 | 46.56 | 0.350 |
| `src/phaze/models/dedup_resolution.py` | 44–53 | — | `src/phaze/models/cloud_job.py` | 10 | 2 | 23.26 | 0.150 |
| `src/phaze/models/discogs_link.py` | 27–36 | — | `src/phaze/models/metadata.py` | 18 | 6 | 54.76 | 0.350 |
| `src/phaze/models/execution.py` | 40–49 | — | `src/phaze/models/tag_write_log.py` | 10 | 2 | 50.00 | 0.350 |
| `src/phaze/models/file.py` | 29–34 | — | `src/phaze/models/filename_convention.py` | 15 | 4 | 54.00 | 0.350 |
| `src/phaze/models/filename_convention.py` | 78–92 | — | `src/phaze/models/file.py` | 15 | 2 | 25.96 | 0.350 |
| `src/phaze/models/metadata.py` | 19–39 | — | `src/phaze/models/analysis.py` | 39 | 9 | 100.00 | 0.350 |
| `src/phaze/models/proposal.py` | 3–13 | — | `src/phaze/models/tag_write_log.py` | 11 | 4 | 31.03 | 0.350 |
| `src/phaze/models/scan_batch.py` | 37–54 | — | `src/phaze/models/analysis.py` | 18 | 6 | 54.10 | 0.350 |
| `src/phaze/models/scheduling_ledger.py` | 77–88 | — | `src/phaze/models/analysis.py` | 16 | 3 | 16.67 | 0.150 |
| `src/phaze/models/stage_skip.py` | 35–45 | — | `src/phaze/models/scan_batch.py` | 12 | 3 | 42.86 | 0.350 |
| `src/phaze/models/tag_write_log.py` | 5–13 | — | `src/phaze/models/proposal.py` | 11 | 6 | 60.47 | 0.350 |
| `src/phaze/models/tracklist.py` | 166–174 | — | `src/phaze/models/metadata.py` | 18 | 5 | 20.86 | 0.150 |
| `src/phaze/models/tracklist_drain_arm_state.py` | 73–80 | — | `src/phaze/models/scan_batch.py` | 18 | 1 | 12.31 | 0.150 |
| `src/phaze/routers/admin_agents.py` | 546–558 | — | `src/phaze/routers/admin_agents.py` | 13 | 5 | 8.90 | 0.150 |
| `src/phaze/routers/agent_analysis.py` | 272–302 | — | `src/phaze/routers/agent_metadata.py` | 32 | 32 | 26.52 | 0.600 |
| `src/phaze/routers/agent_execution.py` | 43–52 | — | `src/phaze/services/tag_writer.py` | 14 | 35 | 18.08 | 0.150 |
| `src/phaze/routers/agent_files.py` | 22–31 | — | `tests/conftest.py` | 12 | 17 | 9.20 | 0.350 |
| `src/phaze/routers/agent_metadata.py` | 86–117 | — | `src/phaze/routers/agent_analysis.py` | 32 | 15 | 25.93 | 0.600 |
| `src/phaze/routers/agent_proposals.py` | 30–37 | — | `src/phaze/services/pipeline/analyze.py` | 9 | 18 | 13.85 | 0.150 |
| `src/phaze/routers/agent_push.py` | 71–77 | — | `src/phaze/tasks/controller.py` | 7 | 57 | 11.11 | 0.350 |
| `src/phaze/routers/agent_s3.py` | 26–34 | — | `src/phaze/routers/admin_agents.py` | 9 | 2 | 14.67 | 0.150 |
| `src/phaze/routers/agent_scratch.py` | 17–24 | — | `src/phaze/routers/agent_proposals.py` | 8 | 1 | 17.39 | 0.150 |
| `src/phaze/routers/agent_tag_writes.py` | 8–19 | — | `src/phaze/tasks/proposal.py` | 12 | 24 | 29.63 | 0.350 |
| `src/phaze/routers/cue.py` | 279–304 | — | `src/phaze/routers/cue.py` | 26 | 2 | 14.18 | 0.150 |
| `src/phaze/routers/execution.py` | 456–470 | — | `src/phaze/routers/execution.py` | 21 | 4 | 9.90 | 0.150 |
| `src/phaze/routers/pipeline/__init__.py` | 27–81 | — | `src/phaze/services/pipeline/__init__.py` | 55 | 5 | 49.72 | 0.350 |
| `src/phaze/routers/pipeline/dashboard_stats.py` | 369–425 | — | `src/phaze/routers/pipeline/dashboard_stats.py` | 57 | 3 | 19.55 | 0.150 |
| `src/phaze/routers/pipeline/files.py` | 16–28 | — | `tests/review/routers/test_tags.py` | 13 | 10 | 22.15 | 0.150 |
| `src/phaze/routers/pipeline/skip.py` | 207–228 | — | `tests/integration/test_stage_status_equivalence.py` | 32 | 27 | 14.07 | 0.150 |
| `src/phaze/routers/pipeline/tracklists.py` | 171–187 | — | `src/phaze/routers/pipeline/tracklists.py` | 17 | 8 | 22.67 | 0.150 |
| `src/phaze/routers/pipeline_scans.py` | 44–51 | — | `src/phaze/tasks/agent_worker.py` | 11 | 27 | 8.74 | 0.150 |
| `src/phaze/routers/search.py` | 25–36 | — | `src/phaze/routers/search.py` | 12 | 3 | 21.28 | 0.150 |
| `src/phaze/schemas/agent_analysis.py` | 63–101 | — | `src/phaze/schemas/agent_analysis.py` | 39 | 3 | 23.26 | 0.150 |
| `src/phaze/schemas/agent_execution.py` | 80–105 | — | `src/phaze/schemas/agent_scan_batches.py` | 26 | 1 | 26.53 | 0.350 |
| `src/phaze/schemas/agent_metadata.py` | 64–73 | — | `src/phaze/schemas/agent_analysis.py` | 10 | 1 | 15.15 | 0.150 |
| `src/phaze/schemas/agent_scan_batches.py` | 46–62 | — | `src/phaze/schemas/agent_execution.py` | 26 | 1 | 19.77 | 0.150 |
| `src/phaze/services/agent_client.py` | 548–558 | — | `src/phaze/services/agent_client.py` | 11 | 3 | 8.27 | 0.150 |
| `src/phaze/services/backends/__init__.py` | 91–126 | — | `src/phaze/services/pipeline/__init__.py` | 36 | 2 | 23.53 | 0.150 |
| `src/phaze/services/backends/compute_agent.py` | 261–277 | — | `src/phaze/services/backends/kueue.py` | 24 | 3 | 12.37 | 0.150 |
| `src/phaze/services/backends/kueue.py` | 238–261 | — | `src/phaze/services/backends/compute_agent.py` | 24 | 2 | 8.33 | 0.150 |
| `src/phaze/services/backends/local.py` | 19–35 | — | `src/phaze/services/backends/compute_agent.py` | 17 | 3 | 26.26 | 0.350 |
| `src/phaze/services/collision.py` | 148–157 | — | `src/phaze/services/collision.py` | 10 | 4 | 17.11 | 0.150 |
| `src/phaze/services/pipeline/__init__.py` | 110–148 | — | `src/phaze/routers/pipeline/__init__.py` | 55 | 4 | 41.67 | 0.350 |
| `src/phaze/services/pipeline/orphans.py` | 113–135 | — | `tests/integration/test_orphan_count.py` | 23 | 1 | 15.75 | 0.150 |
| `src/phaze/services/pipeline/pending.py` | 134–145 | — | `src/phaze/services/pipeline/tracklists.py` | 12 | 5 | 12.50 | 0.150 |
| `src/phaze/services/pipeline/reconciliation.py` | 83–89 | — | `src/phaze/services/pipeline/reconciliation.py` | 7 | 1 | 10.22 | 0.150 |
| `src/phaze/services/scan_deletion.py` | 32–49 | — | `tests/discovery/services/test_scan_deletion.py` | 18 | 23 | 40.91 | 0.600 |
| `src/phaze/services/search_queries.py` | 10–17 | — | `src/phaze/services/scan_deletion.py` | 18 | 26 | 21.59 | 0.150 |
| `src/phaze/services/tag_writer.py` | 28–45 | — | `tests/identify/services/test_tracklist_candidate_queue.py` | 18 | 3 | 9.09 | 0.150 |
| `src/phaze/tasks/_shared/agent_bootstrap.py` | 98–112 | — | `src/phaze/tasks/_shared/agent_bootstrap.py` | 15 | 1 | 23.53 | 0.150 |
| `src/phaze/tasks/aborting_reaper.py` | 64–86 | — | `src/phaze/tasks/active_reaper.py` | 23 | 1 | 23.96 | 0.150 |
| `src/phaze/tasks/active_reaper.py` | 95–116 | — | `src/phaze/tasks/aborting_reaper.py` | 23 | 1 | 18.18 | 0.150 |
| `src/phaze/tasks/agent_worker.py` | 432–455 | — | `src/phaze/tasks/controller.py` | 24 | 24 | 9.51 | 0.350 |
| `src/phaze/tasks/companion_read.py` | 24–40 | — | `src/phaze/tasks/cue_write.py` | 17 | 1 | 26.56 | 0.350 |
| `src/phaze/tasks/controller.py` | 334–352 | — | `src/phaze/tasks/agent_worker.py` | 24 | 43 | 12.78 | 0.350 |
| `src/phaze/tasks/cue_write.py` | 26–40 | — | `src/phaze/tasks/companion_read.py` | 17 | 1 | 27.27 | 0.350 |
| `src/phaze/tasks/metadata_extraction.py` | 59–69 | — | `src/phaze/services/tracklist_drain.py` | 11 | 1 | 14.29 | 0.150 |
| `src/phaze/tasks/proposal.py` | 3–15 | — | `tests/agents/routers/test_agent_tag_writes.py` | 13 | 3 | 15.20 | 0.150 |

## 5. Per-file baseline

Full data for every `src/` file — including the non-`.py` files repowise tracks — is in the
committed JSON. The table below is the 30 lowest-scoring `src/phaze/*.py` files, which is where
every wave-2 and wave-3 bead is aimed.

| File | Score | Maint | Perf | nloc | CCN | Nest | dup% | stmt cov% | branch cov% | Deduction total | reachable | history | other |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `src/phaze/agent_watcher/__main__.py` | 2.29 | 6.90 | 10.00 | 207 | 13 | 5 | — | 98.97 | 96.15 | 7.713 | 4.213 | 3.500 | 0.000 |
| `src/phaze/services/analysis.py` | 2.55 | 4.10 | 9.85 | 1161 | 7 | 4 | — | 100.00 | 93.97 | 7.449 | 3.710 | 3.499 | 0.240 |
| `src/phaze/services/review.py` | 3.46 | 6.70 | 8.00 | 736 | 11 | 4 | 5.63 | 97.52 | 95.65 | 6.533 | 3.033 | 3.500 | 0.000 |
| `src/phaze/job_runner.py` | 4.30 | 6.50 | 10.00 | 486 | 22 | 3 | 2.14 | 98.71 | 89.13 | 5.704 | 2.203 | 3.501 | 0.000 |
| `src/phaze/services/kube_staging.py` | 4.30 | 9.00 | 10.00 | 512 | 9 | 4 | — | 98.92 | 95.00 | 5.702 | 2.203 | 3.499 | 0.000 |
| `src/phaze/tasks/metadata_extraction.py` | 4.44 | 7.90 | 10.00 | 54 | 6 | 5 | 14.29 | 100.00 | 100.00 | 5.558 | 2.058 | 3.500 | 0.000 |
| `src/phaze/tasks/execution.py` | 4.47 | 6.70 | 9.30 | 1010 | 12 | 3 | 7.20 | 99.29 | 96.55 | 5.529 | 2.029 | 3.500 | 0.000 |
| `src/phaze/tasks/reenqueue.py` | 4.49 | 7.90 | 10.00 | 1033 | 10 | 3 | 0.75 | 99.68 | 98.65 | 5.513 | 2.013 | 3.500 | 0.000 |
| `src/phaze/services/tracklist_drain.py` | 4.55 | 7.10 | 8.00 | 786 | 13 | 2 | 4.19 | 100.00 | 98.33 | 5.453 | 1.952 | 3.501 | 0.000 |
| `src/phaze/routers/shell.py` | 4.76 | 8.50 | 10.00 | 1051 | 9 | 3 | 2.33 | 96.43 | 91.67 | 5.239 | 1.740 | 3.499 | 0.000 |
| `src/phaze/routers/proposals.py` | 4.79 | 8.80 | 10.00 | 434 | 9 | 3 | 6.89 | 93.55 | 88.89 | 5.205 | 1.705 | 3.500 | 0.000 |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | 4.85 | 8.00 | 10.00 | 726 | 8 | 3 | 3.87 | 96.30 | 91.25 | 5.151 | 1.651 | 3.500 | 0.000 |
| `src/phaze/routers/pipeline_scans.py` | 5.00 | 7.10 | 10.00 | 580 | 6 | 2 | 8.74 | 100.00 | 100.00 | 5.000 | 1.500 | 3.500 | 0.000 |
| `src/phaze/services/dedup.py` | 5.00 | 10.00 | 9.18 | 443 | 13 | 3 | 5.28 | 100.00 | 100.00 | 5.000 | 1.500 | 3.500 | 0.000 |
| `src/phaze/config.py` | 5.15 | 7.30 | 9.30 | 912 | 8 | 3 | — | 100.00 | 98.53 | 4.850 | 1.350 | 3.500 | 0.000 |
| `src/phaze/tasks/_shared/deterministic_key.py` | 5.15 | 8.80 | 10.00 | 171 | 9 | 3 | — | 100.00 | 95.00 | 4.847 | 1.347 | 3.500 | 0.000 |
| `src/phaze/services/cloud_staging.py` | 5.18 | 7.50 | 10.00 | 333 | 10 | 4 | — | 99.03 | 87.50 | 4.819 | 2.699 | 2.120 | 0.000 |
| `src/phaze/routers/cue.py` | 5.27 | 7.80 | 10.00 | 351 | 7 | 2 | 14.18 | 96.77 | 87.50 | 4.728 | 1.229 | 3.499 | 0.000 |
| `src/phaze/tasks/release_awaiting_cloud.py` | 5.37 | 7.90 | 8.00 | 394 | 7 | 3 | 3.10 | 97.97 | 89.29 | 4.631 | 1.131 | 3.500 | 0.000 |
| `src/phaze/services/agent_task_router.py` | 5.39 | 7.30 | 10.00 | 215 | 5 | 5 | — | 96.61 | 90.00 | 4.614 | 2.494 | 2.120 | 0.000 |
| `src/phaze/services/proposal.py` | 5.39 | 9.00 | 9.18 | 420 | 8 | 4 | 1.57 | 99.40 | 97.83 | 4.613 | 1.112 | 3.501 | 0.000 |
| `src/phaze/scripts/download_models.py` | 5.41 | 9.00 | 10.00 | 342 | 4 | 4 | — | 100.00 | 100.00 | 4.588 | 1.088 | 3.500 | 0.000 |
| `src/phaze/services/tracklist_scraper.py` | 5.50 | 9.70 | 10.00 | 384 | 12 | 3 | — | 100.00 | 97.22 | 4.498 | 0.997 | 3.501 | 0.000 |
| `src/phaze/config_backends.py` | 5.54 | 10.00 | 9.30 | 202 | 9 | 2 | 4.15 | 97.17 | 89.29 | 4.460 | 0.960 | 3.500 | 0.000 |
| `src/phaze/tasks/functions.py` | 5.55 | 7.90 | 10.00 | 433 | 7 | 3 | 6.73 | 100.00 | 100.00 | 4.451 | 0.950 | 3.501 | 0.000 |
| `src/phaze/tasks/agent_worker.py` | 5.58 | 8.40 | 9.30 | 359 | 8 | 3 | 9.51 | 96.99 | 92.86 | 4.420 | 0.920 | 3.500 | 0.000 |
| `src/phaze/routers/execution.py` | 5.62 | 8.40 | 10.00 | 602 | 5 | 3 | 9.90 | 98.05 | 97.37 | 4.378 | 0.878 | 3.500 | 0.000 |
| `src/phaze/services/tracklist_priority.py` | 5.62 | 10.00 | 10.00 | 326 | 13 | 2 | 5.59 | 99.11 | 96.15 | 4.383 | 0.883 | 3.500 | 0.000 |
| `src/phaze/services/agent_liveness.py` | 5.67 | 8.40 | 10.00 | 290 | 13 | 4 | — | 100.00 | 100.00 | 4.329 | 2.610 | 1.320 | 0.399 |
| `src/phaze/tasks/controller.py` | 5.70 | 8.40 | 10.00 | 215 | 6 | 3 | 12.78 | 100.00 | 95.45 | 4.300 | 0.800 | 3.500 | 0.000 |

## 6. Deduction by biomarker, repo-wide

| Biomarker | Bucket | Findings | Deduction |
| --- | --- | ---: | ---: |
| `prior_defect` | history_derived | 168 | 140.896 |
| `complex_method` | reachable | 77 | 60.570 |
| `change_entropy` | history_derived | 43 | 45.410 |
| `co_change_scatter` | history_derived | 40 | 37.739 |
| `coverage_gradient` | reachable | 71 | 31.045 |
| `nested_complexity` | reachable | 32 | 29.592 |
| `error_handling` | reachable | 205 | 23.536 |
| `primitive_obsession` | reachable | 129 | 22.321 |
| `function_hotspot` | history_derived | 39 | 21.170 |
| `dry_violation` | reachable | 67 | 16.850 |
| `churn_risk` | history_derived | 33 | 13.902 |
| `large_method` | reachable | 22 | 8.527 |
| `knowledge_loss` | history_derived | 61 | 7.506 |
| `hidden_coupling` | history_derived | 24 | 6.261 |
| `low_cohesion` | reachable | 6 | 2.850 |
| `bumpy_road` | reachable | 14 | 2.750 |
| `complex_conditional` | unclassified | 4 | 1.437 |
| `brain_method` | unclassified | 1 | 0.600 |
| `ownership_risk` | history_derived | 1 | 0.286 |
| `io_in_loop` | performance | 65 | 0.000 |
| `serial_await_in_loop` | performance | 24 | 0.000 |
| `hot_path_sync_io` | performance | 1 | 0.000 |
| `nested_loop_with_io` | performance | 1 | 0.000 |
| **total** | | **1128** | **473.248** |

## 7. The `--allow-missing-baseline` exemption, exercised here and nowhere else

`phaze-bk9el.21` built `just branch-check` to fail **closed** (exit 2) when no baseline exists,
like every other could-not-compare path in that tool. This bead is the one exemption, because it
is the bead that produces the baseline: it cannot be gated on the artifact it exists to create.
The dispatcher's decision (2026-08-21) was that the exemption lives at the **call site**, as a
visible flag, and not as a lenient default — a default exit 0 that measured nothing is the same
defect family as `phaze-jnj90` (a gate producing no coverage) and `phaze-nqawu` (a submit running
no tests), and it would silently green any of the twelve wave-2 beads whose baseline went missing
for a mundane reason.

So the flag appears below, in this bead's own transcript, applied to the four wave-3 refactor
targets — which also usefully records their pre-refactor branch figures:

```console
$ just branch-check --allow-missing-baseline \
    --file src/phaze/services/analysis.py --file src/phaze/routers/shell.py \
    --file src/phaze/routers/pipeline_scans.py --file src/phaze/config.py
⚠️  NOT A COMPARISON: no baseline at .coverage-baseline.json (recorded by phaze-bk9el.1).
   The figures below are this run's branch coverage, with nothing to compare them to.
   Exiting 0 only because --allow-missing-baseline was passed.
Branch coverage for the 4 tracked file(s) this bead touched (base-ref 'main'):
   ✅ src/phaze/services/analysis.py:  93.97%  (uncovered branch lines: 482, 571, 574, 611, 693, 703, 1071)
   ✅ src/phaze/routers/shell.py:  91.67%  (uncovered branch lines: 625, 633, 663, 931, 936, 945)
   ✅ src/phaze/routers/pipeline_scans.py: 100.00%  (all branches covered)
   ✅ src/phaze/config.py:  98.53%  (uncovered branch lines: 260)
$ echo "exit code: $?"
exit code: 0
```

Then, and only then, the baseline was written:

```console
$ just branch-check --write-baseline
✅ Wrote coverage baseline for 258 modules (181 with branches) to .coverage-baseline.json

$ just branch-check --file src/phaze/services/analysis.py --file src/phaze/routers/shell.py
Branch coverage for the 2 tracked file(s) this bead touched (base-ref 'main'):
   ✅ src/phaze/services/analysis.py:  93.97% vs baseline  93.97%  (+0.00) — uncovered branch lines: 482, 571, 574, 611, 693, 703, 1071
   ✅ src/phaze/routers/shell.py:  91.67% vs baseline  91.67%  (+0.00) — uncovered branch lines: 625, 633, 663, 931, 936, 945

✅ No touched file lowered its branch coverage against the baseline.
```

**No other bead should pass `--allow-missing-baseline`.** If you are reading this because your
wave-2 bead failed with exit 2, the baseline is `.coverage-baseline.json` on the epic branch —
fetch it, do not waive the check.

## 8. Reproducing this baseline

```bash
# health/finding side (reads the Repowise index; --db defaults to the script's own checkout)
uv run scripts/health_baseline.py \
    --db /path/to/durable/checkout/.repowise/wiki.db \
    --coverage-json coverage.json \
    --baseline-commit 65e5af46e5812969445b1d6c4aaf09a8b1a4b5d7 \
    --index-commit 8d1bf08cf1bdb7a6075784872c45404c09cbe897 \
    --out /tmp/rerun.json
diff docs/spikes/phaze-bk9el.1-health-baseline-2026-08-21.json /tmp/rerun.json

# branch-coverage side
just test-cov && just branch-check           # compares against .coverage-baseline.json
```

Output is deterministic — files sorted by path, every nested key sorted — so a diff between two
runs reflects real index changes only. Run against the same index, it reproduces byte for byte:

```console
$ uv run scripts/health_baseline.py --db .../.repowise/wiki.db --coverage-json coverage.json \
      --baseline-commit 65e5af46e5812969445b1d6c4aaf09a8b1a4b5d7 \
      --index-commit 8d1bf08cf1bdb7a6075784872c45404c09cbe897 --out /tmp/rerun.json
$ echo "exit code: $?"
exit code: 0
$ diff docs/spikes/phaze-bk9el.1-health-baseline-2026-08-21.json /tmp/rerun.json
$ echo "diff exit code: $?"
diff exit code: 0
$ wc -l /tmp/rerun.json
   17624 /tmp/rerun.json
```

Empty diff, run and captured — both invocations exited 0 and emitted the same 17624-line document.

**Why `--baseline-commit` exists.** The `analyzed_commit` field comes from
`repositories.head_commit`, which `repowise init` sets and `repowise update` never refreshes — and
`health_file_metrics.analyzed_commit` is NULL for all 382 rows here. Neither can be trusted to name
the commit a baseline describes, so criterion 6's named commit is passed in explicitly and recorded
as `baseline_commit`. `index_commit` records the checkout the index was built from, which is the
different (and equally necessary) fact.

`scripts/health_baseline.py` was extended by this bead rather than duplicated: it now also emits
`max_ccn`, `max_nesting`, `nloc`, `duplication_pct`, `branch_coverage_pct`, per-finding
**deduction** (not just counts), this epic's reachable/history-derived split, the per-file DRY
records, and an optional merge of authoritative coverage from a `coverage json` report. Every key
`phaze-vu88k.1` emitted is still emitted with the same meaning, so the two artifacts remain
diffable on their shared keys; byte-reproducing the vu88k artifact needs that commit's copy of the
script (`git show ef1ef7d2:scripts/health_baseline.py`) and, more importantly, its index state,
which the re-ingest above has deliberately replaced.

## 9. Provenance: which tool produced each figure, and why it is not stale

`phaze-bk9el.9` reported that repowise served **byte-identical numbers across two commits and two
`repowise update` runs**, with a frozen `health_analyzed_at` and a frozen `nloc`, and read that as
an MCP-layer cache. Since this bead *is* the measurement every downstream comparison is made
against, "some of these numbers might describe an earlier tree" is the one defect it cannot ship.
So the route behind every figure is recorded here, and the freshness is established rather than
assumed.

### Route table

| Figures | Route | Cached? |
| --- | --- | --- |
| Statement / branch / combined coverage, per module and repo-wide; `.coverage-baseline.json` | `coverage.json` from coverage.py, this bead's own suite run | no — not a repowise artifact at all |
| Ingest counts (31150 records, 258 files) | `repowise coverage add` stdout + `scripts/repowise_coverage_gate.py` re-reading `repowise coverage status --format json` / `repowise status --format json` | no — CLI, in-process, and the gate re-reads stored state rather than trusting an exit code |
| `score`, `maintainability_score`, `performance_score`, `max_ccn`, `max_nesting`, `nloc`, `duplication_pct`, `line_coverage_pct`, `branch_coverage_pct` | `scripts/health_baseline.py` → **direct sqlite read** of `.repowise/wiki.db` (`health_file_metrics`) | no — reads the stored rows themselves, below any tool layer |
| Every finding, its `health_impact`, its `details_json` (including the DRY clone partners) | same, `health_findings` | same |

**No figure in this artifact came from the MCP tools.** `get_health`, `get_context` and `get_risk`
were not used to produce any of it.

### Why the stored rows are fresh

The rows this artifact reads were written by **this bead's own `repowise health` run**, as step 5
of the five-step refresh:

```console
$ sqlite3 .repowise/wiki.db "SELECT min(updated_at), max(updated_at) FROM health_file_metrics"
2026-08-22 00:20:45.607060|2026-08-22 00:20:45.677882      -- 2342 rows
$ sqlite3 .repowise/wiki.db "SELECT min(created_at), max(created_at) FROM health_findings"
2026-08-22 00:20:45.892728|2026-08-22 00:20:46.053056      -- 3290 rows
$ sqlite3 .repowise/wiki.db "SELECT max(ingested_at) FROM coverage_files"
2026-08-22 00:19:59.060313                                  -- 258 rows
```

Every findings row has `created_at == updated_at` at that instant: the fold did not update rows in
place, it **recreated** them. That is a stronger statement than "not cached".

### Three independent routes agree exactly

The same values were re-taken through the non-cached CLI path and through the MCP path and compared
field by field against the committed JSON:

| Cross-check | Scope | Mismatches |
| --- | --- | ---: |
| `repowise health --file <path> --format json` | 22 files × 9 fields (`score`, `max_ccn`, `max_nesting`, `nloc`, `duplication_pct`, `line_coverage_pct`, `branch_coverage_pct`, total deduction, finding count) = **198 values** | **0** |
| MCP `get_health` | 3 files × the same 9 fields = **27 values** | **0** |

The 22 files span the score range: the 12 worst, the four wave-3 refactor targets,
`routers/duplicates.py` (the one module whose coverage verdict differs between metrics),
`routers/agent_analysis.py` (the file `phaze-bk9el.9` measured), three mid-range and three
top-scoring files. MCP `get_health`'s `_meta.health_analyzed_at` came back as
`2026-08-22T00:20:45.677882` — **exactly this bead's fold timestamp** — with
`indexed_commit: 8d1bf08cf1bd`, `index_behind: false`, and
`coverage.summary.ingested_commit_sha: 8d1bf08cf1bdb7a6075784872c45404c09cbe897`.

### The mechanism is not an MCP cache — measured

Running `repowise update` on an unchanged tree leaves **all three tables byte-identical**:

```console
$ # before
health_file_metrics max(updated_at) = 2026-08-22 00:20:45.677882
health_findings     max(created_at) = 2026-08-22 00:20:46.053056
coverage_files      max(ingested_at)= 2026-08-22 00:19:59.060313
$ repowise update          # exit 0
$ # after — identical, to the microsecond
health_file_metrics max(updated_at) = 2026-08-22 00:20:45.677882
health_findings     max(created_at) = 2026-08-22 00:20:46.053056
coverage_files      max(ingested_at)= 2026-08-22 00:19:59.060313
```

So `repowise update` **does not run the health fold at all** — it advances the structural index and
nothing else. A frozen `health_analyzed_at` across two updates is therefore the correct, faithful
behaviour of a tool reporting rows that nobody recomputed; the numbers were stale because
`repowise health` had not been run, not because a cache served old ones.

This matters for the workaround the epic adopts. **"Avoid the MCP tool" does not fix it** — the MCP
tool, the CLI dashboard and a direct sqlite read all serve the same stored rows, and this artifact's
three-way agreement above is the demonstration. What fixes it is running the fold: `repowise health`,
or `just repowise-coverage`, which runs all five steps and fails closed if any of them silently
no-ops. `repowise health --file <path>` gave `phaze-bk9el.9` a genuinely different answer because it
recomputes that one file in-process, which is a real and useful property — but it is a recompute, not
a cache bypass, and it does not refresh what anyone else subsequently reads.

This is the same defect family as criterion 1 of this very bead: `repowise update` does not
re-ingest coverage either. Two halves of one gap — the structural index refreshes on its own, and
neither coverage nor health does.
